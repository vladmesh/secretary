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

from secretary.config import ConfigError, load_config
from secretary.sprints import SprintReader
from secretary.tasks import KanboardClient
from triggered_agents.runtime.paths import default_instance_path, instance_dir

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

ROUTE_UNKNOWN = "unknown"
ROUTE_GLOBAL = "global"
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def selected_instance() -> Path:
    """The installation whose registry is authoritative for curator routing."""
    return Path(os.environ.get("SECRETARY_INSTANCE", str(default_instance_path()))).expanduser()


def _normalized_directory(value: str | Path, *, strict: bool) -> Path | None:
    """Return a resolved directory, never treating a missing or unreadable path as a route."""
    try:
        path = Path(value).expanduser().resolve(strict=strict)
        return path if path.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def project_bindings(instance: Path | None = None) -> list[dict]:
    """Read usable canonical bindings from the selected instance registry.

    A malformed or unreadable entry is deliberately not a partial route.  `id`, `repo`, and
    `orca_binding` are all needed to distinguish a checkout from its Orca workspace tree.
    """
    root = instance_dir(instance or selected_instance())
    directory = root / "projects"
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            binding = load_config(path)
        except ConfigError:
            continue
        if not isinstance(binding, dict):
            continue
        project_id, repo, orca_binding = (
            binding.get("id"),
            binding.get("repo"),
            binding.get("orca_binding"),
        )
        if (
            not all(isinstance(value, str) and value for value in (project_id, repo, orca_binding))
            or not _PROJECT_ID.fullmatch(project_id)
            or not Path(repo).is_absolute()
            or Path(orca_binding).name != orca_binding
            or orca_binding in {".", ".."}
        ):
            continue
        result.append({"id": project_id, "repo": repo, "orca_binding": orca_binding})
    return result


def registered_project_ids(instance: Path | None = None) -> set[str]:
    """Canonical ids which a curator selector may name, without deriving ids from paths."""
    return {binding["id"] for binding in project_bindings(instance)}


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _workspace_root() -> Path:
    return Path(os.environ.get("TA_WORKSPACES_ROOT") or Path.home() / "orca" / "workspaces")


def _observer_reference(cwd: Path) -> str | None:
    """Extract an observer's sprint reference only from its canonical workspace shape."""
    root = _normalized_directory(_workspace_root() / "observers", strict=False)
    if root is None or not _within(cwd, root):
        return None
    relative = cwd.relative_to(root)
    if not relative.parts:
        return None
    name = relative.parts[0]
    if not name.startswith("sprint-") or len(name) == len("sprint-"):
        return None
    return name[len("sprint-") :]


def _observer_route(reference: str, instance: Path, known_ids: set[str]) -> str:
    """Resolve an observer only from its sprint's single structured reservation."""
    try:
        sprint = SprintReader(KanboardClient.for_instance(instance)).show(
            reference, include_cards=False, include_resume_freshness=False
        )
    # Board reachability is not curator work.  A failure to read the structured record is
    # deliberately an unknown route, never an exception that suppresses other discovery.
    except Exception:
        return ROUTE_UNKNOWN
    reservations = sprint.get("reservations") if isinstance(sprint, dict) else None
    if not isinstance(reservations, list) or len(reservations) != 1 or not isinstance(reservations[0], str):
        return ROUTE_UNKNOWN
    return reservations[0] if reservations[0] in known_ids else ROUTE_UNKNOWN


def resolve_route(cwd: str, *, instance: Path | None = None, global_source: bool = False) -> str:
    """Return a canonical project id, or the explicit `unknown`/`global` route.

    Registered checkout descendants and the corresponding Orca workspace descendants are valid
    routes.  The candidate must match exactly one resolved boundary, so prefixes, duplicate
    bindings, missing repositories, and symlink spellings cannot produce a guessed owner.
    """
    if global_source:
        return ROUTE_GLOBAL
    candidate = _normalized_directory(cwd, strict=False) if cwd else None
    if candidate is None:
        return ROUTE_UNKNOWN
    instance = instance_dir(instance or selected_instance())
    bindings = project_bindings(instance)
    known_ids = {binding["id"] for binding in bindings}
    reference = _observer_reference(candidate)
    if reference is not None:
        return _observer_route(reference, instance, known_ids)

    root = _normalized_directory(_workspace_root(), strict=False)
    matches = []
    for binding in bindings:
        repo = _normalized_directory(binding["repo"], strict=True)
        workspace = _normalized_directory(root / binding["orca_binding"], strict=False) if root else None
        if (repo and _within(candidate, repo)) or (workspace and _within(candidate, workspace)):
            matches.append(binding["id"])
    return matches[0] if len(matches) == 1 else ROUTE_UNKNOWN


def _with_route(source: dict, *, global_source: bool = False) -> dict:
    return {**source, "route": resolve_route(source.get("cwd", ""), global_source=global_source)}

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
            out.append(_with_route({"head": "claude", "path": str(f), "session_id": f.stem, "cwd": cwd}))
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
        out.append(_with_route({"head": "hermes", "path": str(HERMES_STATE_DB), "session_id": session_id, "cwd": cwd}))
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
        out.append(_with_route({"head": "codex", "path": str(f), "session_id": meta["session_id"], "cwd": cwd}))
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
            out.append(_with_route({"head": "claude", "path": str(f), "cwd": cwd}))
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
            out.append(_with_route({"head": "hermes", "path": str(f), "cwd": ""}, global_source=True))
    return out


def all_memory_files() -> list[dict]:
    """All discoverable personal-memory files across heads."""
    return claude_memory_files() + hermes_memory_files()


if __name__ == "__main__":
    for s in all_sessions():
        print(s["head"], s["session_id"], s["cwd"], s["path"])
    for m in all_memory_files():
        print(m["head"], "memory", m["cwd"], m["path"])
