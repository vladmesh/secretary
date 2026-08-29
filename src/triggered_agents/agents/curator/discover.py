"""Discovery — where each head writes its raw session files and personal-memory facts.

Paths mirror Orca's ai-vault session-scanner (src/main/ai-vault/session-scanner-*),
which we reuse as reference rather than runtime (it only keeps 5-message previews and is
reachable only in-process). We read the raw files ourselves. Claude, Hermes and Codex
sessions on this host are wired; add a parser + path as new heads produce sessions.

Self-exclusion: the curator excludes only its own exact workspace and, where the
launcher provides one, its exact session id. It deliberately does not exclude the
Secretary base checkout, sibling worker worktrees, or observer workspaces.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

# Claude project-dir naming: every non-alphanumeric cwd character becomes "-".
# Overridable via TA_CLAUDE_PROJECTS_DIR so a run (e.g. an e2e on fixtures) can point the
# scan at a synthetic tree instead of the live ~/.claude/projects.
CLAUDE_PROJECTS = Path(os.environ.get("TA_CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))

# Hermes home. Overridable via TA_HERMES_HOME_DIR for the same reason as
# TA_CLAUDE_PROJECTS_DIR above. On this host Hermes 0.17.0 stores sessions in a shared
# SQLite DB (hermes_state.py: "replacing the per-session JSONL file approach") rather than
# the per-session `session_*.json` files under a `sessions/` dir that the Orca ai-vault
# scanner (our format reference) still expects -- that dir exists but is always empty here.
# We read the live schema instead of the stale file-based reference.
HERMES_HOME = Path(os.environ.get("TA_HERMES_HOME_DIR", str(Path.home() / ".hermes")))
HERMES_STATE_DB = HERMES_HOME / "state.db"
HERMES_MEMORY_DIR = HERMES_HOME / "memories"

# Orca-managed Codex home shared by interactive Orca heads and pipeline Codex heads.
# Overridable in tests for the same reason as CLAUDE_PROJECTS/HERMES_HOME.
CODEX_SESSIONS = Path(
    os.environ.get(
        "TA_CODEX_SESSIONS_DIR",
        str(Path.home() / ".config" / "orca" / "codex-runtime-home" / "home" / "sessions"),
    )
)

def curator_workspace() -> Path:
    """The one workspace this curator run must not feed back into itself."""
    return Path(os.environ.get("TA_CURATOR_WORKSPACE") or Path.cwd()).resolve(strict=False)


def curator_session_id() -> str:
    return os.environ.get("TA_CURATOR_SESSION_ID", "")


def _cwd_from_claude_dir(dirname: str) -> str:
    # "-home-dev-secretary" -> "/home/dev/secretary". Lossy (dirs with real
    # dashes collide); only a fallback when the file carries no cwd field.
    return "/" + dirname.lstrip("-").replace("-", "/")


def _cwd_from_file(path: Path, fallback: str) -> str:
    # Claude JSONL lines carry the real `cwd`; read the first that has it (dashes intact).
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(10):
                line = fh.readline()
                if not line:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except json.JSONDecodeError:
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return fallback


def _excluded(cwd: str, session_id: str = "") -> bool:
    if session_id and session_id == curator_session_id():
        return True
    if not cwd:
        return False
    try:
        return Path(cwd).resolve(strict=False) == curator_workspace()
    except (OSError, ValueError):
        return False


def _dirname_for_cwd(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _excluded_dirname(name: str) -> bool:
    return name == _dirname_for_cwd(str(curator_workspace()))


def claude_sessions() -> list[dict]:
    """List Claude session files as {head, path, session_id, cwd}, self-excluded."""
    out = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        fallback = _cwd_from_claude_dir(proj.name)
        for f in sorted(proj.glob("*.jsonl")):
            cwd = _cwd_from_file(f, fallback)
            if _excluded(cwd, f.stem):
                continue
            out.append({"head": "claude", "path": str(f), "session_id": f.stem, "cwd": cwd})
    return out


def _hermes_query(sql: str, params: tuple = ()) -> list[tuple] | None:
    """Run one read-only query against state.db. Returns None (not []) on any sqlite
    failure -- a corrupted or transiently write-locked DB must degrade Hermes discovery
    to empty, not raise and take down the whole harvest tick, including the unrelated
    Claude side."""
    try:
        # Read-only: state.db is live-written by real Hermes sessions (WAL mode per its
        # own docstring, concurrent readers are safe) -- the curator only ever reads it.
        con = sqlite3.connect(f"file:{HERMES_STATE_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()


def hermes_sessions() -> list[dict]:
    """List Hermes sessions from ~/.hermes/state.db as {head, path, session_id, cwd},
    self-excluded by cwd like claude_sessions(). `path` is the shared state.db for every
    row -- unlike Claude's one-file-per-session layout, harvest.py watermarks Hermes by
    session_id, not by this path."""
    out = []
    if not HERMES_STATE_DB.is_file():
        return out
    rows = _hermes_query("SELECT id, cwd FROM sessions WHERE archived = 0 ORDER BY id")
    if not rows:
        return out
    for session_id, cwd in rows:
        cwd = cwd or ""
        if _excluded(cwd, session_id):
            continue
        out.append({"head": "hermes", "path": str(HERMES_STATE_DB), "session_id": session_id, "cwd": cwd})
    return out


def _codex_meta_from_file(path: Path) -> dict:
    """Read Codex's session_meta line. Returns session_id/cwd fallbacks on bad files."""
    meta = {"session_id": path.stem, "cwd": ""}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "session_meta":
                    continue
                payload = rec.get("payload") or {}
                meta["session_id"] = payload.get("session_id") or payload.get("id") or path.stem
                meta["cwd"] = payload.get("cwd") or ""
                break
    except OSError:
        pass
    return meta


def codex_sessions() -> list[dict]:
    """List Codex session JSONL files as {head, path, session_id, cwd}, self-excluded."""
    out = []
    if not CODEX_SESSIONS.is_dir():
        return out
    for f in sorted(CODEX_SESSIONS.glob("**/*.jsonl")):
        meta = _codex_meta_from_file(f)
        cwd = meta["cwd"]
        if _excluded(cwd, meta["session_id"]):
            continue
        out.append({"head": "codex", "path": str(f), "session_id": meta["session_id"], "cwd": cwd})
    return out


def hermes_messages(
    session_id: str, since_id: int = 0, limit: int = 512, max_content_bytes: int = 65536
) -> list[dict]:
    """Return {id, role, content, timestamp} rows for one Hermes session, id > since_id.

    `active = 1` matches hermes_state.py's own default message-load filter: a /rollback
    (checkpoint restore) soft-deletes superseded messages by flipping active to 0 rather
    than removing the row, and those never became conversation the user acted on.
    """
    if not HERMES_STATE_DB.is_file():
        return []
    rows = _hermes_query(
        "SELECT id, role, CASE WHEN length(CAST(content AS BLOB)) <= ? THEN content ELSE '' END, "
        "timestamp, length(CAST(content AS BLOB)) FROM messages "
        "WHERE session_id = ? AND id > ? AND active = 1 ORDER BY id LIMIT ?",
        (max_content_bytes, session_id, since_id, limit),
    )
    if not rows:
        return []
    return [
        {"id": r[0], "role": r[1], "content": r[2], "timestamp": r[3], "content_bytes": r[4] or 0}
        for r in rows
    ]


def all_sessions() -> list[dict]:
    """All discoverable sessions across heads."""
    return claude_sessions() + hermes_sessions() + codex_sessions()


def claude_memory_files() -> list[dict]:
    """List personal-memory markdown files as {head, path, cwd}, self-excluded.

    One file per durable memory a head chose to keep, under
    `~/.claude/projects/<project>/memory/*.md`. `MEMORY.md` is the index for that
    memory, not a fact — skipped everywhere, not just for excluded projects.
    """
    out = []
    if not CLAUDE_PROJECTS.is_dir():
        return out
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
        if not proj.is_dir():
            continue
        mem_dir = proj / "memory"
        if not mem_dir.is_dir():
            continue
        if _excluded_dirname(proj.name):
            continue
        cwd = _cwd_from_claude_dir(proj.name)
        session_files = sorted(proj.glob("*.jsonl"))
        if session_files:
            cwd = _cwd_from_file(session_files[0], cwd)
        if _excluded(cwd):
            continue
        for f in sorted(mem_dir.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            out.append({"head": "claude", "path": str(f), "cwd": cwd})
    return out


def hermes_memory_files() -> list[dict]:
    """List Hermes's built-in personal-memory files (MEMORY.md, USER.md) as {head, path, cwd}.

    Unlike Claude, Hermes keeps ONE global pair of files for the whole install (see
    tools/memory_tool.py in hermes-agent: MemoryStore reads/writes `<hermes home>/memories/
    {MEMORY,USER}.md`, entries delimited by a "section sign" separator line) -- not scoped
    per-project, so there is no cwd to self-exclude on here. cwd is reported as "" (global);
    the curator applies its usual durable-fact bar to judge relevance instead of a
    project-path filter.
    """
    out = []
    if not HERMES_MEMORY_DIR.is_dir():
        return out
    for name in ("MEMORY.md", "USER.md"):
        f = HERMES_MEMORY_DIR / name
        if f.is_file():
            out.append({"head": "hermes", "path": str(f), "cwd": ""})
    return out


def all_memory_files() -> list[dict]:
    """All discoverable personal-memory files across heads."""
    return claude_memory_files() + hermes_memory_files()


if __name__ == "__main__":
    for s in all_sessions():
        print(s["head"], s["session_id"], s["cwd"], s["path"])
    for m in all_memory_files():
        print(m["head"], "memory", m["cwd"], m["path"])
