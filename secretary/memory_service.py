"""Memory MCP server, shared semantic memory for any MCP-speaking agent.

SQLite + sqlite-vec for storage/ANN, fastembed (bge-m3, multilingual) for embeddings,
exposed over streamable-HTTP so Claude / Codex / Hermes all share ONE warm instance.
"""

import hashlib
import json
import os
import sqlite3
import datetime
import subprocess
import threading
import time
import tempfile
from typing import Any
from pathlib import Path

import numpy as np
import sqlite_vec
import yaml
from fastembed import TextEmbedding
from mcp.server.fastmcp import FastMCP

HERE = Path(__file__).parent
DEFAULT_MEMORY_DIR = Path.home() / "secretary-data" / "memory"
# Canon lives in the private instance repo (docs/RECOVERY.md, "Layout"); the
# export and the vector index stay derived under the data dir.
DEFAULT_CANON = Path.home() / "secretary-instance" / "state" / "memory" / "facts"

if "MEMORY_CANON_ROOT" in os.environ:
    CANON = Path(os.environ["MEMORY_CANON_ROOT"])
elif "PANELMEM_KB" in os.environ:
    CANON = Path(os.environ["PANELMEM_KB"]) / "memory"
else:
    CANON = DEFAULT_CANON

DB_PATH = os.environ.get("MEMORY_DB", str(DEFAULT_MEMORY_DIR / "index.sqlite"))
MODEL = os.environ.get("MEMORY_MODEL", "intfloat/multilingual-e5-large")
PORT = int(os.environ.get("MEMORY_PORT", "8077"))
DIM = int(os.environ.get("MEMORY_DIM", "1024"))
MODEL_CACHE_DIR = Path(os.environ.get("MEMORY_CACHE_DIR", str(DEFAULT_MEMORY_DIR / "fastembed-cache")))
THREADS = int(os.environ.get("MEMORY_THREADS", "1"))
SEARCH_LOG = os.environ.get("MEMORY_SEARCH_LOG", str(Path(DB_PATH).parent / "search-log.jsonl"))
CANON_EXPORT = Path(os.environ["MEMORY_CANON_EXPORT"]) if "MEMORY_CANON_EXPORT" in os.environ else CANON.parent / "export.ndjson"
WATCH_INTERVAL = float(os.environ.get("MEMORY_WATCH_INTERVAL", "10"))

_embedder = None
_lock = threading.Lock()
_reindex_lock = threading.Lock()
_search_log_lock = threading.Lock()  # own lock: don't couple log I/O to embedder/reindex locks
_ready_event = threading.Event()
_ready_error = None


def embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        with _lock:
            if _embedder is None:
                _embedder = TextEmbedding(
                    model_name=MODEL,
                    cache_dir=str(MODEL_CACHE_DIR),
                    threads=THREADS,
                )
    return _embedder


def _unit(vec) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def embed_doc(text: str) -> np.ndarray:
    return _unit(list(embedder().embed([text]))[0])


def embed_query(text: str) -> np.ndarray:
    return _unit(list(embedder().query_embed(text))[0])


def db(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def init_db(path: str | Path = DB_PATH) -> None:
    conn = db(path)
    create_schema(conn)
    conn.commit()
    conn.close()


def create_schema(conn: sqlite3.Connection, dim: int | None = None) -> None:
    dim = DIM if dim is None else dim
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memories("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, fact_id TEXT UNIQUE, content_hash TEXT, "
        "text TEXT NOT NULL, scope TEXT, tags TEXT, source TEXT, created_at TEXT)"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(embedding float[{dim}])"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS index_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


class NotReadyError(RuntimeError):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class IndexReadError(RuntimeError):
    pass


def not_ready_response(reason: str, detail: str) -> dict:
    return {
        "status": "not_ready",
        "error": reason,
        "retryable": True,
        "detail": detail,
    }


def index_exists() -> bool:
    return Path(DB_PATH).is_file()


def search_ready() -> bool:
    return _ready_event.is_set() and index_exists()


def mark_search_ready(error: Exception | None = None) -> None:
    global _ready_error
    _ready_error = error
    _ready_event.set()


def mark_search_not_ready(error: Exception | None = None) -> None:
    global _ready_error
    _ready_error = error
    _ready_event.clear()


def add_memory(text: str, tags=None, source=None) -> int:
    conn = db()
    cur = conn.execute(
        "INSERT INTO memories(text, tags, source, created_at) VALUES (?,?,?,?)",
        (text, tags, source, datetime.datetime.utcnow().isoformat()),
    )
    rid = cur.lastrowid
    conn.execute(
        "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
        (rid, sqlite_vec.serialize_float32(embed_doc(text).tolist())),
    )
    conn.commit()
    conn.close()
    return rid


def normalize_scope(scope: str | None) -> str | None:
    """Accept "global", "project:<dir>" or a bare project dir name ("orca" → "project:orca")."""
    if not scope:
        return None
    scope = scope.strip()
    if scope in ("", "global") or scope.startswith("project:"):
        return scope or None
    return f"project:{scope}"


def search_memory(query: str, k: int = 5, scope: str | None = None) -> list:
    if not index_exists():
        raise NotReadyError("index_missing", f"index does not exist: {DB_PATH}")
    scope = normalize_scope(scope)
    # sqlite-vec KNN can't push a join filter into MATCH, so with a scope we over-fetch
    # and trim post-hoc. Fine at canon size (hundreds of facts).
    fetch = max(k * 5, 25) if scope else k
    qvec = sqlite_vec.serialize_float32(embed_query(query).tolist())
    with _reindex_lock:  # don't read while a rebuild is swapping tables
        conn = db()
        rows = conn.execute(
            "SELECT v.rowid, v.distance, m.text, m.scope, m.tags, m.source, m.created_at "
            "FROM vec_memories v JOIN memories m ON m.id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (qvec, fetch),
        ).fetchall()
        conn.close()
    if scope:
        rows = [r for r in rows if r[3] == scope][:k]
    # unit vectors → L2 distance d relates to cosine: cos = 1 - d^2/2
    return [
        {
            "id": r[0],
            "score": round(1 - (r[1] ** 2) / 2, 4),
            "text": r[2],
            "scope": r[3],
            "tags": r[4],
            "source": r[5],
            "created_at": r[6],
        }
        for r in rows
    ]


def log_search(query: str, k: int, results: list,
               scope: str | None = None, caller: str | None = None) -> None:
    """Append one jsonl line per memory_search call. Best-effort telemetry:
    any failure here is swallowed. Logging must never break the search.
    caller is self-reported by the agent (role name), attribution, not auth."""
    try:
        entry = {
            "ts": datetime.datetime.utcnow().isoformat(),
            "query": query,
            "k": k,
            "hits": [{"id": r["id"], "score": r["score"]} for r in results],
        }
        if scope:
            entry["scope"] = scope
        if caller:
            entry["caller"] = caller
        line = json.dumps(entry, ensure_ascii=False)
        with _search_log_lock:  # one write per line so concurrent calls don't interleave
            with open(SEARCH_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
    except Exception:
        pass


# ── Canon → index (daemon-owned reindex) ──────────────────────────────────────
# The production canon is the private instance repo's state/memory/facts.
# Prefer the atomically published export.ndjson snapshot; rollback to the old
# panelmem-kb repo is one setting, MEMORY_CANON_ROOT=/home/dev/panelmem-kb/memory,
# and then we read HEAD.


def scope_for_relative(path: Path) -> str:
    top = path.parts[0]
    return "global" if top == "global" else f"project:{top}"


def parse_frontmatter(raw: str) -> tuple[dict, str]:
    meta, body = {}, raw
    if raw.startswith("---"):
        _, front, body = raw.split("---", 2)
        meta = yaml.safe_load(front) or {}
    return meta, body


def parse_fact_text(raw: str, path: str | Path, fact_id: str | None = None) -> dict:
    rel = Path(path)
    meta, body = parse_frontmatter(raw)
    tags = meta.get("tags")
    if isinstance(tags, str):
        tag_text = tags
    else:
        tag_text = ",".join(tags) if tags else None
    return {
        "id": fact_id or str(rel.with_suffix("")),
        "path": str(rel),
        "slug": rel.stem,
        "scope": scope_for_relative(rel),
        "text": body.strip(),
        "tags": tag_text,
        "source": meta.get("source"),
        "created_at": str(meta["created"]) if meta.get("created") else None,
        "meta": meta,
    }


def deleted_fact(meta: dict) -> bool:
    status = str(meta.get("status") or meta.get("state") or "").strip().lower()
    return (
        meta.get("deleted") is True
        or meta.get("tombstone") is True
        or meta.get("active") is False
        or status in {"deleted", "superseded", "removed", "tombstone"}
    )


def supersede_refs(meta: dict) -> list[str]:
    value = meta.get("supersedes")
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []


def fact_ref_matches(fact: dict, ref: str) -> bool:
    ref_path = Path(ref)
    if "/" in ref:
        normalized = str(ref_path.with_suffix(""))
        return normalized in {
            fact["id"],
            str(Path(fact["path"]).with_suffix("")),
            fact["path"],
        }
    return ref in {fact["slug"], fact["id"], str(Path(fact["path"]).with_suffix(""))}


def filter_current_facts(facts: list[dict]) -> list[dict]:
    active = [fact for fact in facts if not deleted_fact(fact["meta"])]
    superseded: list[tuple[str, str]] = []
    for fact in active:
        superseded.extend((fact["id"], ref) for ref in supersede_refs(fact["meta"]))
    current = []
    for fact in active:
        removed = any(owner != fact["id"] and fact_ref_matches(fact, ref) for owner, ref in superseded)
        if not removed:
            current.append(fact)
    return current


def load_export_snapshot(path: Path) -> list[dict]:
    facts = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            raw = obj.get("text", "")
            rel = obj.get("path") or f"{obj['id']}.md"
            facts.append(parse_fact_text(raw, rel, fact_id=obj.get("id")))
    return facts


def _git(path: Path, *args: str) -> list[str]:
    """Build a Git command safe for a root recovery over an owned checkout."""
    # Recovery intentionally runs as root while bootstrap gives the instance
    # checkout to the installation user.  Git must trust that checkout for every
    # read in the canon snapshot, not merely for the initial clone/fetch.
    return ["git", "-c", "safe.directory=*", "-C", str(path), *args]


def git_repo_root(path: Path) -> Path | None:
    proc = subprocess.run(
        _git(path, "rev-parse", "--show-toplevel"),
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def load_git_head_snapshot(path: Path) -> list[dict]:
    root = git_repo_root(path)
    if root is None:
        raise RuntimeError(f"canon snapshot unavailable: {path} is not a git worktree")
    prefix = path.resolve().relative_to(root.resolve())
    proc = subprocess.run(
        _git(root, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", str(prefix)),
        check=True,
        stdout=subprocess.PIPE,
    )
    facts = []
    for raw_name in proc.stdout.split(b"\0"):
        if not raw_name:
            continue
        repo_rel = raw_name.decode("utf-8")
        if not repo_rel.endswith(".md"):
            continue
        rel = Path(repo_rel).relative_to(prefix)
        show = subprocess.run(
            _git(root, "show", f"HEAD:{repo_rel}"),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        facts.append(parse_fact_text(show.stdout, rel, fact_id=str(rel.with_suffix(""))))
    return facts


def load_canon_entries(canon: Path | None = None, export: Path | None = None) -> list[dict]:
    canon = canon or CANON
    export = export or CANON_EXPORT
    if export.is_file():
        return load_export_snapshot(export)
    if not canon.is_dir():
        return []
    return load_git_head_snapshot(canon)


def load_canon(canon: Path | None = None, export: Path | None = None) -> list:
    return filter_current_facts(load_canon_entries(canon, export))


def canon_signature() -> tuple:
    """Cheap change token: (file count, max mtime, total size). Catches add/edit/delete."""
    if CANON_EXPORT.is_file():
        st = CANON_EXPORT.stat()
        return ("export", st.st_mtime, st.st_size)
    root = git_repo_root(CANON) if CANON.is_dir() else None
    if root is not None:
        proc = subprocess.run(
            _git(root, "rev-parse", "HEAD"),
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        return ("git", proc.stdout.strip())
    if not CANON.is_dir():
        return (0, 0.0, 0)
    raise RuntimeError(f"canon snapshot unavailable: no {CANON_EXPORT} and {CANON} is not in git")


def build_document_embedder(model: str, cache_dir: str | Path | None = None):
    """Return a normalized document embedder for an explicit model."""
    embedding_model = TextEmbedding(
        model_name=model,
        cache_dir=str(cache_dir) if cache_dir is not None else None,
    )

    class DocumentEmbedder:
        def __call__(self, text: str) -> np.ndarray:
            return self.embed_many([text])[0]

        def embed_many(self, texts: list[str]) -> list[np.ndarray]:
            return [
                _unit(vector)
                for vector in embedding_model.embed(texts, batch_size=2)
            ]

    return DocumentEmbedder()


def fact_content_hash(fact: dict) -> str:
    """Hash all indexed fields so metadata-only changes are not missed."""
    payload = {key: fact.get(key) for key in ("text", "scope", "tags", "source", "created_at")}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def indexed_fact_count(path: str | Path | None = None) -> int:
    path = Path(path or DB_PATH)
    if not path.is_file():
        return 0
    conn = None
    try:
        conn = db(path)
        return conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    except sqlite3.DatabaseError as error:
        raise IndexReadError(f"index unreadable: {path}: {error}") from error
    finally:
        if conn is not None:
            conn.close()


def write_index(facts: list[dict], target: Path, model: str, dim: int, document_embed) -> int:
    """Write a complete index to a temporary file, then atomically publish it."""
    embed_many = getattr(document_embed, "embed_many", None)
    vectors = (
        embed_many([fact["text"] for fact in facts])
        if callable(embed_many)
        else [document_embed(fact["text"]) for fact in facts]
    )
    if len(vectors) != len(facts):
        raise RuntimeError(
            f"embedder returned {len(vectors)} vectors for {len(facts)} facts"
        )
    rows = list(zip(facts, vectors))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.unlink(missing_ok=True)
    try:
        conn = db(tmp)
        create_schema(conn, dim)
        conn.executemany(
            "INSERT INTO index_metadata(key, value) VALUES (?, ?)",
            [("model", model), ("dimension", str(dim)), ("schema", "2")],
        )
        for f, vec in rows:
            cur = conn.execute(
                "INSERT INTO memories(fact_id, content_hash, text, scope, tags, source, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    f["id"], fact_content_hash(f), f["text"], f["scope"], f["tags"],
                    f["source"], f["created_at"],
                ),
            )
            conn.execute(
                "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, sqlite_vec.serialize_float32(vec.tolist())),
            )
        conn.commit()
        conn.close()
        indexed = indexed_fact_count(tmp)
        if indexed != len(facts):
            raise RuntimeError(f"index parity failed before publish: expected {len(facts)}, got {indexed}")
        with _reindex_lock:
            os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return indexed_fact_count(target)


def index_metadata(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        return {}
    conn = None
    try:
        conn = db(path)
        rows = conn.execute("SELECT key, value FROM index_metadata").fetchall()
    except sqlite3.OperationalError as error:
        if "no such table: index_metadata" in str(error):
            return {}
        raise IndexReadError(f"index unreadable: {path}: {error}") from error
    except sqlite3.DatabaseError as error:
        raise IndexReadError(f"index unreadable: {path}: {error}") from error
    finally:
        if conn is not None:
            conn.close()
    return {key: value for key, value in rows}


def index_compatibility(path: str | Path, model: str, dim: int) -> tuple[str, str | None]:
    try:
        metadata = index_metadata(path)
    except IndexReadError as error:
        return "unusable", str(error)
    if not metadata:
        return "legacy", None
    if metadata.get("model") != model or metadata.get("dimension") != str(dim):
        return "mismatch", (
            f"index metadata mismatch: expected model={model!r} dimension={dim}, "
            f"got model={metadata.get('model')!r} dimension={metadata.get('dimension')!r}"
        )
    return "compatible", None


def offline_rebuild(
    canon: str | Path,
    export: str | Path,
    target_db: str | Path,
    model: str,
    dim: int,
    document_embed=None,
    allow_empty: bool = False,
) -> dict:
    """Build and atomically publish an index from an explicit canon snapshot.

    ``document_embed`` is an injection point for tests. Production callers omit it,
    which loads the requested fastembed model.
    """
    canon_path = Path(canon)
    export_path = Path(export)
    target = Path(target_db)
    if int(dim) <= 0:
        raise ValueError(f"dimension must be positive: {dim}")
    if not export_path.is_file() and not canon_path.is_dir():
        raise RuntimeError(
            f"canon snapshot unavailable: no export at {export_path} and no canon at {canon_path}"
        )
    facts = load_canon(canon_path, export_path)
    if not facts and not allow_empty:
        raise RuntimeError("canon snapshot has no current facts; pass allow_empty=True to publish an empty index")
    document_embed = document_embed or build_document_embedder(model)
    indexed = write_index(facts, target, model, int(dim), document_embed)
    parity = {"expected": len(facts), "indexed": indexed, "ok": indexed == len(facts)}
    if not parity["ok"]:
        raise RuntimeError(f"index parity failed after publish: expected {len(facts)}, got {indexed}")
    return {
        "ok": parity["ok"],
        "canon": str(canon_path),
        "export": str(export_path),
        "target_db": str(target),
        "model": model,
        "dimension": int(dim),
        "parity": parity,
    }


def incremental_update(
    canon: str | Path = CANON,
    export: str | Path = CANON_EXPORT,
    target_db: str | Path = DB_PATH,
    model: str = MODEL,
    dim: int = DIM,
    document_embed=None,
    allow_empty: bool = True,
) -> dict:
    """Update added, changed and deleted facts in a compatible index.

    Legacy and mismatched indexes are rebuilt atomically. Changed embeddings are
    computed before the transaction, so an embedding failure preserves the old index.
    """
    canon_path = Path(canon)
    export_path = Path(export)
    target = Path(target_db)
    facts = load_canon(canon_path, export_path)
    if not facts and not allow_empty:
        raise RuntimeError("canon snapshot has no current facts")
    compatibility, _ = index_compatibility(target, model, int(dim))
    if compatibility != "compatible":
        rebuilt = offline_rebuild(
            canon_path, export_path, target, model, int(dim), document_embed, allow_empty
        )
        return {
            **rebuilt, "mode": "rebuild", "added": len(facts), "updated": 0,
            "deleted": 0, "reused": 0,
        }

    conn = db(target)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        if not {"fact_id", "content_hash"}.issubset(columns):
            conn.close()
            rebuilt = offline_rebuild(
                canon_path, export_path, target, model, int(dim), document_embed, allow_empty
            )
            return {
                **rebuilt, "mode": "rebuild", "added": len(facts), "updated": 0,
                "deleted": 0, "reused": 0,
            }
        existing = {
            fact_id: (rowid, content_hash)
            for rowid, fact_id, content_hash in conn.execute(
                "SELECT id, fact_id, content_hash FROM memories"
            )
        }
        desired = {fact["id"]: (fact, fact_content_hash(fact)) for fact in facts}
        added = [item for key, item in desired.items() if key not in existing]
        changed = [
            item for key, item in desired.items()
            if key in existing and existing[key][1] != item[1]
        ]
        deleted = [key for key in existing if key not in desired]
        document_embed = document_embed or build_document_embedder(model)
        vectors = {
            fact["id"]: document_embed(fact["text"])
            for fact, _ in (*added, *changed)
        }
        with _reindex_lock:
            conn.execute("BEGIN IMMEDIATE")
            for fact_id in deleted:
                rowid = existing[fact_id][0]
                conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (rowid,))
                conn.execute("DELETE FROM memories WHERE id = ?", (rowid,))
            for fact, digest in changed:
                rowid = existing[fact["id"]][0]
                conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (rowid,))
                conn.execute(
                    "UPDATE memories SET content_hash=?, text=?, scope=?, tags=?, source=?, "
                    "created_at=? WHERE id=?",
                    (
                        digest, fact["text"], fact["scope"], fact["tags"], fact["source"],
                        fact["created_at"], rowid,
                    ),
                )
                conn.execute(
                    "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                    (rowid, sqlite_vec.serialize_float32(vectors[fact["id"]].tolist())),
                )
            for fact, digest in added:
                cur = conn.execute(
                    "INSERT INTO memories(fact_id, content_hash, text, scope, tags, source, "
                    "created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        fact["id"], digest, fact["text"], fact["scope"], fact["tags"],
                        fact["source"], fact["created_at"],
                    ),
                )
                conn.execute(
                    "INSERT INTO vec_memories(rowid, embedding) VALUES (?, ?)",
                    (cur.lastrowid, sqlite_vec.serialize_float32(vectors[fact["id"]].tolist())),
                )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    indexed = indexed_fact_count(target)
    if indexed != len(facts):
        raise RuntimeError(f"incremental index parity failed: expected {len(facts)}, got {indexed}")
    return {
        "ok": True, "mode": "incremental", "canon": str(canon_path),
        "export": str(export_path), "target_db": str(target), "model": model,
        "dimension": int(dim), "added": len(added), "updated": len(changed),
        "deleted": len(deleted), "reused": len(facts) - len(added) - len(changed),
        "parity": {"expected": len(facts), "indexed": indexed, "ok": True},
    }


def rebuild_index() -> int:
    """Daemon rebuild using its configured canon, model, and target database."""
    result = offline_rebuild(
        CANON, CANON_EXPORT, DB_PATH, MODEL, DIM, document_embed=embed_doc, allow_empty=True
    )
    return result["parity"]["indexed"]


def update_index() -> dict:
    """Daemon-owned incremental update using the warm document embedder."""
    return incremental_update(
        CANON, CANON_EXPORT, DB_PATH, MODEL, DIM, document_embed=embed_doc, allow_empty=True
    )


def parity_gate() -> dict:
    expected = len(load_canon())
    indexed = indexed_fact_count(DB_PATH)
    return {"ok": expected == indexed, "expected": expected, "indexed": indexed}


def bootstrap_index() -> int | None:
    """Warm the embedder and reconcile the index incrementally."""
    mark_search_not_ready()
    embedder()
    try:
        result = update_index()
        n = result["parity"]["indexed"]
    except Exception as e:
        has_index = index_exists()
        status, detail = index_compatibility(DB_PATH, MODEL, DIM) if has_index else ("missing", None)
        if has_index and status in {"compatible", "legacy"}:
            mark_search_ready(e)
            print(
                f"memory-mcp: incremental reconciliation failed, keeping previous index: {e}",
                flush=True,
            )
            return None
        error = RuntimeError(detail) if detail else e
        mark_search_not_ready(error)
        print(
            f"memory-mcp: incremental reconciliation failed, no compatible index available: {error}",
            flush=True,
        )
        return None
    status, detail = index_compatibility(DB_PATH, MODEL, DIM)
    if status != "compatible":
        error = detail or "incrementally reconciled index has no metadata"
        mark_search_not_ready(RuntimeError(error))
        print(f"memory-mcp: incrementally reconciled index is incompatible: {error}", flush=True)
        return None
    mark_search_ready()
    return n


def start_background_bootstrap() -> None:
    def loop():
        try:
            n = bootstrap_index()
            if n is not None:
                print(
                    f"memory-mcp ready: model={MODEL} dim={DIM} db={DB_PATH} port={PORT} "
                    f"facts={n} canon={CANON} export={CANON_EXPORT} watch={WATCH_INTERVAL}s",
                    flush=True,
                )
        finally:
            start_canon_watcher()

    threading.Thread(target=loop, name="bootstrap-index", daemon=True).start()


def start_canon_watcher() -> None:
    """Background thread: rebuild the index whenever the canon changes on disk."""
    def loop():
        last = canon_signature()
        while True:
            time.sleep(WATCH_INTERVAL)
            try:
                sig = canon_signature()
                if sig != last:
                    result = update_index()
                    n = result["parity"]["indexed"]
                    mark_search_ready()
                    last = sig
                    print(
                        "memory-mcp: canon changed, "
                        f"indexed={n} added={result['added']} updated={result['updated']} "
                        f"deleted={result['deleted']} reused={result['reused']}",
                        flush=True,
                    )
            except Exception as e:  # never let the watcher kill the daemon
                print(f"memory-mcp: watcher error: {e}", flush=True)

    threading.Thread(target=loop, name="canon-watcher", daemon=True).start()


mcp = FastMCP("memory", host="127.0.0.1", port=PORT)

# Память read-only для агентов: пишет только куратор (markdown-канон → reindex).
# Внутренний add_memory оставлен для миграции/selftest, но через MCP не светится.


@mcp.tool()
def memory_search(query: str, k: int = 5, scope: str = "", caller: str = "") -> Any:
    """Semantic search over shared memory. Returns up to k closest entries with scores.

    scope: optional filter, "global", "project:<dir>" or bare project dir name
    (e.g. "orca", "secretary"). Search your own project's scope first,
    then retry without scope. k is clamped to 10. caller: your role (worker/reviewer/
    steward/secretary/curator), telemetry only, always pass it."""
    # Кап выдачи (решение vladmesh 2026-07-11): скоры у ранжировщика лежат в узкой полке
    # (~0.80-0.84 по телеметрии), длинный хвост неотличим от топа и засоряет контекст.
    # В лог пишем исходный k: телеметрия должна видеть, что просили на самом деле.
    requested_k = k
    k = max(1, min(k, 10))
    if not search_ready():
        reason = "embedder_loading" if _ready_error is None else "index_unavailable"
        detail = "embedding model/index are still loading"
        if _ready_error is not None:
            detail = str(_ready_error)
        return not_ready_response(reason, detail)
    try:
        results = search_memory(query, k, scope=scope or None)
    except NotReadyError as e:
        return not_ready_response(e.reason, e.detail)
    # tool-level: capture real agent queries, not internal calls
    log_search(query, requested_k, results, scope=normalize_scope(scope), caller=caller or None)
    return results


@mcp.tool()
def memory_get(id: int) -> dict:
    """Fetch one memory entry by id."""
    if not index_exists():
        return not_ready_response("index_missing", f"index does not exist: {DB_PATH}")
    conn = db()
    r = conn.execute(
        "SELECT id, text, scope, tags, source, created_at FROM memories WHERE id = ?", (id,)
    ).fetchone()
    conn.close()
    if not r:
        return {"error": "not found", "id": id}
    return {"id": r[0], "text": r[1], "scope": r[2], "tags": r[3], "source": r[4], "created_at": r[5]}


@mcp.tool()
def memory_list(limit: int = 50) -> list:
    """List recent memory entries (newest first)."""
    if not index_exists():
        return [not_ready_response("index_missing", f"index does not exist: {DB_PATH}")]
    conn = db()
    rows = conn.execute(
        "SELECT id, text, scope, tags, source, created_at FROM memories ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "text": r[1], "scope": r[2], "tags": r[3], "source": r[4], "created_at": r[5]}
        for r in rows
    ]


def main() -> None:
    print(
        f"secretary memory MCP listening: port={PORT} db={DB_PATH} canon={CANON}; "
        "index warming in background",
        flush=True,
    )
    start_background_bootstrap()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
