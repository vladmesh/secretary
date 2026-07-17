"""Harvest — turn new session lines and personal-memory files into a redacted batch for the
curator agent.

Two-phase by design: `harvest(st)` reads everything new since the watermark and returns a
batch WITHOUT advancing the watermark. The agent extracts facts and commits the canon,
then calls `advance(st, pending)` to move the watermark. A crash between the two re-harvests
the same turns next run (at worst a duplicate the agent dedups), never a silent drop.

Signal only: user text + assistant text blocks. Dropped as noise — thinking blocks,
tool_use/tool_result payloads, meta/sidechain lines, Codex context injection, and local-command wrappers (the
`<local-command-*>` and `<command-*>` scaffolding Claude Code injects, not real turns).

Personal-memory files are a second, independent source: whole-file reads instead of line
turns, watermarked by (mtime, size) instead of a line count, but sharing the same watermark
dict and the same two-phase harvest/advance handshake. The curator never edits or deletes
these files — read-only in, canon out.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ...runtime.redact import redact
from . import discover

# Local-command / harness scaffolding that shows up as user "messages" but isn't a turn.
_SCAFFOLD = re.compile(r"<(local-command|command-name|command-message|command-args)[\s>]")
_CODEX_CONTEXT_PREFIXES = (
    "# AGENTS.md instructions for ",
    "<environment_context>",
)


def _text_from_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def parse_claude_lines(lines) -> list[dict]:
    """Extract user/assistant text turns from Claude JSONL lines."""
    turns = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        if d.get("isMeta") or d.get("isSidechain"):
            continue
        msg = d.get("message") or {}
        text = _text_from_content(msg.get("content")).strip()
        if not text or _SCAFFOLD.search(text):
            continue
        turns.append({"role": msg.get("role", d["type"]), "text": text, "ts": d.get("timestamp")})
    return turns


def _text_from_codex_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("input_text", "output_text", "text"):
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def parse_codex_lines(lines) -> list[dict]:
    """Extract user/assistant text turns from Codex JSONL response items."""
    turns = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "response_item":
            continue
        payload = d.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _text_from_codex_content(payload.get("content")).strip()
        if not text or _SCAFFOLD.search(text) or text.startswith(_CODEX_CONTEXT_PREFIXES):
            continue
        turns.append({"role": role, "text": text, "ts": d.get("timestamp")})
    return turns


def _iso_ts(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def parse_hermes_rows(rows) -> list[dict]:
    """Extract user/assistant text turns from Hermes state.db message rows.

    Tool-call rows (role="tool", and assistant rows with empty content while a tool
    call is in flight) are dropped the same way Claude's tool_use/tool_result blocks
    are dropped in parse_claude_lines -- signal only.
    """
    turns = []
    for row in rows:
        role = row.get("role")
        if role not in ("user", "assistant"):
            continue
        text = (row.get("content") or "").strip()
        if not text or _SCAFFOLD.search(text):
            continue
        turns.append({"role": role, "text": text, "ts": _iso_ts(row.get("timestamp"))})
    return turns


def _completed_jsonl_lines(text: str) -> tuple[list[str], int]:
    """Return only newline-terminated JSONL records."""
    lines = text.splitlines()
    if text and not text.endswith("\n"):
        return lines[:-1], max(len(lines) - 1, 0)
    return lines, len(lines)


def _harvest_jsonl_sessions(mark: dict, sessions: list[dict], parse_lines) -> tuple[list[dict], dict]:
    sessions_out, pending = [], {}
    for sess in sessions:
        path = Path(sess["path"])
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        prev = mark.get(sess["path"], {})
        prev_lines = prev.get("lines", 0)
        if prev.get("mtime") == stat.st_mtime and prev.get("size") == stat.st_size and prev_lines:
            continue  # unchanged since last run
        text = path.read_text(encoding="utf-8", errors="replace")
        complete_lines, complete_count = _completed_jsonl_lines(text)
        new_lines = complete_lines[prev_lines:]
        pending[sess["path"]] = {"lines": complete_count, "mtime": stat.st_mtime, "size": stat.st_size}
        if not new_lines:
            continue
        turns = parse_lines(new_lines)
        for t in turns:
            t["text"] = redact(t["text"])
        if turns:
            sessions_out.append({**sess, "turns": turns})
    return sessions_out, pending


def _harvest_claude_sessions(mark: dict) -> tuple[list[dict], dict]:
    return _harvest_jsonl_sessions(mark, discover.claude_sessions(), parse_claude_lines)


def _harvest_codex_sessions(mark: dict) -> tuple[list[dict], dict]:
    return _harvest_jsonl_sessions(mark, discover.codex_sessions(), parse_codex_lines)


def _harvest_hermes_sessions(mark: dict) -> tuple[list[dict], dict]:
    """Watermarked by session_id -> last message id read, not by (path, mtime)/lines --
    Hermes sessions share one SQLite file (discover.HERMES_STATE_DB) that is written
    continuously by every live session, so file-level mtime/line-count doesn't identify
    which session actually has new turns."""
    sessions_out, pending = [], {}
    for sess in discover.hermes_sessions():
        key = f"hermes:{sess['session_id']}"
        since_id = mark.get(key, {}).get("last_id", 0)
        rows = discover.hermes_messages(sess["session_id"], since_id)
        if not rows:
            continue
        pending[key] = {"last_id": rows[-1]["id"]}
        turns = parse_hermes_rows(rows)
        for t in turns:
            t["text"] = redact(t["text"])
        if turns:
            sessions_out.append({**sess, "turns": turns})
    return sessions_out, pending


def harvest_memory_files(mark: dict) -> tuple[list[dict], dict]:
    """Read new/changed personal-memory files since the watermark.

    Watermarked by (mtime, size) per path — files are read whole, there is no line
    count to resume from. Returns (entries, pending) with the same pending-dict shape
    `advance()` already merges for sessions.
    """
    entries, pending = [], {}
    for mem in discover.all_memory_files():
        path = Path(mem["path"])
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        prev = mark.get(mem["path"], {})
        if prev.get("mtime") == stat.st_mtime and prev.get("size") == stat.st_size:
            continue  # unchanged since last run
        pending[mem["path"]] = {"mtime": stat.st_mtime, "size": stat.st_size}
        text = path.read_text(encoding="utf-8", errors="replace")
        entries.append({**mem, "text": redact(text)})
    return entries, pending


def harvest(st) -> dict:
    """Read all new turns and changed memory files since the watermark. Does NOT advance
    the watermark.

    Returns {"sessions": [...], "memory": [...], "pending": {source_key: watermark}}
    where pending is the combined watermark to persist via advance() after the canon commit.
    """
    mark = st.load_watermark()
    claude_out, claude_pending = _harvest_claude_sessions(mark)
    hermes_out, hermes_pending = _harvest_hermes_sessions(mark)
    codex_out, codex_pending = _harvest_codex_sessions(mark)
    pending = {**claude_pending, **hermes_pending, **codex_pending}
    memory_out, mem_pending = harvest_memory_files(mark)
    pending.update(mem_pending)
    return {"sessions": claude_out + hermes_out + codex_out, "memory": memory_out, "pending": pending}


def advance(st, pending: dict) -> None:
    """Persist the watermark after a successful canon commit."""
    mark = st.load_watermark()
    mark.update(pending)
    st.save_watermark(mark)


def render_markdown(batch: dict) -> str:
    """Human/agent-readable batch of transcripts and personal-memory files. Secrets already
    redacted."""
    sessions = batch["sessions"]
    memory = batch.get("memory", [])
    if not sessions and not memory:
        return "# Нет новых ходов с прошлого прогона.\n"
    lines = ["# Батч транскриптов для куратора", ""]
    for sess in sessions:
        lines.append(f"## {sess['head']} · `{sess['cwd']}` · session {sess['session_id'][:8]}")
        lines.append("")
        for t in sess["turns"]:
            who = "**Юзер**" if t["role"] == "user" else "**Агент**"
            ts = f" _{t['ts']}_" if t.get("ts") else ""
            lines.append(f"{who}{ts}:")
            lines.append(t["text"])
            lines.append("")
    if memory:
        lines.append("# Личная память голов (новое/изменённое)")
        lines.append("")
        for m in memory:
            lines.append(f"## {m['head']} · `{m['cwd']}` · `{Path(m['path']).name}`")
            lines.append("")
            lines.append(m["text"])
            lines.append("")
    return "\n".join(lines)
