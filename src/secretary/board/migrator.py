"""The board store's migration runner: numbered SQL files, applied once, under a lock.

§7.4 of ``docs/BOARD_STORE.md`` decides the shape and argues against a framework: the whole
migration need is "apply numbered SQL files in order, once, under a lock", and a framework would
add a DSL, a second configuration file and a second CLI to an installation whose dependency list
is deliberately short.  So this is that runner and nothing more.

Four properties are load-bearing and each is tested:

* each migration runs **inside its own transaction together with its ``schema_migrations`` row**,
  so PostgreSQL's transactional DDL leaves the version it was moving from rather than half a
  schema;
* a **checksum mismatch on an already-applied version is a hard refusal**, never a re-apply — an
  edited historical migration means the tree and the running schema disagree, and guessing which
  is right is how a store loses data;
* the runner holds ``pg_advisory_lock`` on a fixed key, so two upgrades cannot race;
* the passwords ``0001`` needs are **runner parameters**, substituted as SQL literals at apply
  time, so every installation's migration file is byte-identical and its checksum means something.

``psycopg`` is imported inside the functions that talk to a server.  Everything above that — the
file discovery, the checksums, the plan and the version assertion — is ordinary Python over a
connection object, which is why it is testable without a database and why importing this module
costs nothing on a host whose venv has not been reinstalled yet.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secretary.board.store import BoardStoreError

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: The schema version this build of the product speaks.  It is the highest migration shipped in
#: the tree, and `assert_schema_version` compares a running database against it.
EXPECTED_SCHEMA_VERSION = 1

#: A fixed 64-bit key, so every runner of every checkout contends on the same lock.  Any constant
#: would do; this one is the first 63 bits of sha256("secretary.board.migrations"), recorded here
#: as a literal rather than recomputed, because a key that changes with a hashing detail is not a
#: fixed key.
ADVISORY_LOCK_KEY = 0x2C5B1F4A6E9D0713

#: The parameters `0001` declares, spelled the way §5.5's fence spells them.
PASSWORD_PARAMETERS = ("app_password", "read_password")

_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_PARAMETER = re.compile(r":'([a-z_]+)'")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    @property
    def parameters(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_PARAMETER.findall(self.sql)))


def checksum(text: str) -> str:
    """The checksum stored in ``schema_migrations``: sha256 of the file as shipped.

    Of the file, not of what runs: the substituted passwords differ per installation, and a
    checksum that differed with them could never be compared across two installations.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """Every ``NNNN_name.sql`` in the product tree, in version order."""
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        matched = _FILENAME.match(path.name)
        if not matched:
            raise BoardStoreError(f"migration file name is not NNNN_name.sql: {path.name}")
        version = int(matched.group(1))
        if version in migrations:
            raise BoardStoreError(f"two migrations claim version {version}")
        text = path.read_text(encoding="utf-8")
        migrations[version] = Migration(version, matched.group(2), path, text, checksum(text))
    ordered = tuple(migrations[version] for version in sorted(migrations))
    for position, migration in enumerate(ordered, 1):
        if migration.version != position:
            raise BoardStoreError(
                f"migration versions must run 1..N without a gap; found {migration.version} at {position}"
            )
    return ordered


def applied(conn: Any) -> dict[int, str]:
    """``{version: checksum}`` of what the database has already applied.

    An empty database has no ``schema_migrations`` table at all — `0001` is what creates it — so
    its absence is "nothing applied", not an error.
    """
    row = conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone()
    if row is None or row[0] is None:
        return {}
    rows = conn.execute("SELECT version, checksum FROM schema_migrations ORDER BY version").fetchall()
    return {int(version): str(value) for version, value in rows}


def plan(migrations: tuple[Migration, ...], recorded: dict[int, str]) -> tuple[Migration, ...]:
    """The migrations still owed, after refusing every disagreement between tree and database."""
    shipped = {migration.version: migration for migration in migrations}
    for version, stored in sorted(recorded.items()):
        migration = shipped.get(version)
        if migration is None:
            raise BoardStoreError(
                f"the database has applied migration {version:04d}, which this product tree does not "
                "ship; the running schema is ahead of the code"
            )
        if migration.checksum != stored:
            raise BoardStoreError(
                f"migration {migration.path.name} was edited after it was applied: the database "
                f"recorded checksum {stored} and the tree now hashes to {migration.checksum}; "
                "the running schema and the tree disagree and neither may be guessed at"
            )
    return tuple(migration for migration in migrations if migration.version not in recorded)


def render(migration: Migration, passwords: dict[str, str]) -> str:
    """The SQL actually sent: the file, with its declared parameters as SQL literals.

    The quoting is PostgreSQL's own — a doubled quote inside a single-quoted string — which is
    what ``psycopg.sql.Literal`` produces for a text value; it is done here rather than through
    ``sql.SQL(...).format`` because the DDL contains ``'{}'::jsonb`` defaults that a format
    string would read as placeholders of its own.
    """
    declared = migration.parameters
    unknown = [name for name in declared if name not in PASSWORD_PARAMETERS]
    if unknown:
        raise BoardStoreError(
            f"{migration.path.name} declares parameters this runner does not supply: " + ", ".join(unknown)
        )
    missing = [name for name in declared if not passwords.get(name)]
    if missing:
        raise BoardStoreError(
            f"{migration.path.name} needs the generated {', '.join(missing)}; the runner was given none"
        )
    text = migration.sql
    for name in declared:
        literal = "'" + passwords[name].replace("'", "''") + "'"
        text = text.replace(f":'{name}'", literal)
    return text


def current_version(conn: Any) -> int | None:
    """The highest version the database has applied, or ``None`` for an unmigrated database."""
    recorded = applied(conn)
    return max(recorded) if recorded else None


def assert_schema_version(conn: Any, expected: int = EXPECTED_SCHEMA_VERSION) -> int:
    """Refuse to proceed against a schema this build does not speak (§7.4).

    Deliberately **not** wired into any write path by this card: no consumer reads SQL yet, and
    the process that asserts it is the SQL ``BoardHost``, which is a later card.  It exists, and
    it is what that card calls.
    """
    version = current_version(conn)
    if version is None:
        raise BoardStoreError(
            f"the board store has no schema at all; this build expects version {expected:04d}, "
            "so run the migrations before writing"
        )
    if version != expected:
        raise BoardStoreError(
            f"the board store is at schema version {version:04d} and this build expects "
            f"{expected:04d}; refusing to write through a schema it does not know"
        )
    return version


def apply(
    conn: Any,
    *,
    passwords: dict[str, str],
    migrations: tuple[Migration, ...] | None = None,
    applied_at: Any = None,
) -> tuple[int, ...]:
    """Apply every owed migration in order, under the advisory lock.  Returns what it applied.

    The connection must be an owner connection with autocommit off (§5.5: the application role
    has no DDL).  The lock is session-level, so it spans the per-migration transactions and is
    released once at the end whatever happened in between.
    """
    owed_source = discover() if migrations is None else migrations
    stamp = applied_at or datetime.now(UTC)
    conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    conn.commit()
    try:
        owed = plan(owed_source, applied(conn))
        conn.commit()
        done: list[int] = []
        for migration in owed:
            try:
                conn.execute(render(migration, passwords))
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at, checksum) "
                    "VALUES (%s, %s, %s, %s)",
                    (migration.version, migration.name, stamp, migration.checksum),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            done.append(migration.version)
        return tuple(done)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        conn.commit()


def connect_owner(credentials: Any) -> Any:
    """An owner connection for the runner, from `board_store.resolve_role(instance, "owner")`."""
    import psycopg

    if credentials.role != "owner":
        raise BoardStoreError(f"the migration runner needs the owner role, not {credentials.role!r} (§5.5)")
    return psycopg.connect(credentials.conninfo(), autocommit=False)


__all__ = [
    "ADVISORY_LOCK_KEY",
    "EXPECTED_SCHEMA_VERSION",
    "MIGRATIONS_DIR",
    "PASSWORD_PARAMETERS",
    "Migration",
    "applied",
    "apply",
    "assert_schema_version",
    "checksum",
    "connect_owner",
    "current_version",
    "discover",
    "plan",
    "render",
]
