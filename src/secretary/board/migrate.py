"""Applying the board store's schema: Alembic, under the §7.4 advisory lock.

The owner decided on 2026-09-07 that the schema is SQLAlchemy models and the migrations are
Alembic's, replacing the in-product runner the earlier draft of ``docs/BOARD_STORE.md`` §7.4
described.  What this module keeps from that decision is everything §7.4 asked for that Alembic
does not do by itself:

* **the lock.**  ``pg_advisory_lock`` on a fixed key, taken on the *same session* the migrations
  run on and released once at the end, so two upgrades racing on one installation serialize
  instead of both migrating.  Alembic has no opinion about concurrent runners.
* **the connection.**  §5.4's ``board-store.env``, resolved per §5.5 role, never an
  ``sqlalchemy.url`` literal in an ini file — and therefore behind the git-exclusion enforcement
  `board_store.resolve` performs.
* **the two generated passwords.**  §5.5's ``CREATE ROLE`` statements take them as parameters of
  the run, handed to the revision through ``config.attributes``; they are not bytes of any
  revision file.
* **`upgrade.py`'s three outcomes.**  Unconfigured is a no-op, configured and current is
  unchanged, configured and broken is a failure carrying its reason — every one of them reaching
  the caller as a single `BoardStoreError`.

What it no longer does: version numbers, checksums and a rule about editing an applied file.
Alembic's ``alembic_version`` table is the version, and inventing bookkeeping on top of it is
exactly what the owner refused.

``sqlalchemy`` and ``alembic`` are imported inside the functions that need them, for the reason
``psycopg`` was: the upgrade that installs the dependencies has to be able to start on a venv
that does not have them yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from secretary.board import store as board_store
from secretary.board.store import BoardStoreError

#: Alembic's script directory, shipped inside the package.
SCRIPT_LOCATION = Path(__file__).resolve().parent / "migrations"

#: The revision this build of the product expects a store to be at.  It is Alembic's head, and
#: `head_revision()` reads it from the script directory rather than trusting this literal.
#: `0006_sprint_transport_key` is the head serving collision-free Sprint dispatch.
EXPECTED_SCHEMA_REVISION = "0006_sprint_transport_key"

#: A fixed 64-bit key, so every runner of every checkout contends on the same lock.  Any constant
#: would do; this one is the first 63 bits of sha256("secretary.board.migrations"), recorded here
#: as a literal rather than recomputed, because a key that changes with a hashing detail is not a
#: fixed key.
ADVISORY_LOCK_KEY = 0x2C5B1F4A6E9D0713


def sqlalchemy_url(credentials: Any) -> Any:
    """A SQLAlchemy URL for one §5.5 role, over the `psycopg` driver §5.8 chose.

    Built with ``URL.create`` rather than by formatting a string, so a generated password
    containing ``@``, ``/`` or ``:`` produces one URL and not a truncated one.
    """
    from sqlalchemy.engine import URL

    return URL.create(
        "postgresql+psycopg",
        username=credentials.user,
        password=credentials.password,
        host=credentials.host,
        port=credentials.port,
        database=credentials.dbname,
    )


def alembic_config(*, connection: Any = None, passwords: dict[str, str] | None = None) -> Any:
    """Alembic's `Config`, built in code and carrying no connection string of its own.

    There is no ``alembic.ini`` in this product on purpose: the only URL an installation has is
    §5.4's, and a second place to write one is a second authority.  The connection and the two
    generated passwords travel in ``attributes``, which is Alembic's supported channel for
    exactly this.
    """
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    if connection is not None:
        config.attributes["connection"] = connection
    if passwords is not None:
        config.attributes["passwords"] = dict(passwords)
    return config


def script_directory() -> Any:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config())


def head_revision() -> str:
    """The revision the shipped script directory ends at."""
    head = script_directory().get_current_head()
    if head is None:
        raise BoardStoreError("the board store ships no migrations at all")
    return head


def current_revision(connection: Any) -> str | None:
    """What the database says it is at, or ``None`` for a database Alembic has never touched."""
    from alembic.migration import MigrationContext

    return MigrationContext.configure(connection).get_current_revision()


def pending(connection: Any) -> tuple[str, ...]:
    """The revisions this database still owes, oldest first."""
    script = script_directory()
    current = current_revision(connection)
    owed = [revision.revision for revision in script.iterate_revisions("heads", current)]
    owed.reverse()
    return tuple(owed)


def assert_schema_revision(connection: Any, expected: str | None = None) -> str:
    """Refuse to proceed against a schema this build does not speak (§7.4).

    Deliberately **not** wired into any write path by this card: no consumer reads SQL yet, and
    the process that asserts it is the SQL ``BoardHost``, which is a later card.  It exists, and
    it is what that card calls.
    """
    wanted = expected or head_revision()
    revision = current_revision(connection)
    if revision is None:
        raise BoardStoreError(
            f"the board store has no schema at all; this build expects revision {wanted}, "
            "so run the migrations before writing"
        )
    if revision != wanted:
        raise BoardStoreError(
            f"the board store is at schema revision {revision} and this build expects {wanted}; "
            "refusing to write through a schema it does not know"
        )
    return revision


def apply(connection: Any, *, passwords: dict[str, str], dry_run: bool = False) -> tuple[str, ...]:
    """Upgrade one owner connection to head under the advisory lock.  Returns what it applied.

    The lock is session-level, so it spans Alembic's per-revision transactions and is released
    once at the end whatever happened in between.  A dry run takes the same lock and reads the
    same version table, and returns what it *would* apply without running a revision.
    """
    from alembic import command

    connection.exec_driver_sql("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    connection.commit()
    try:
        owed = pending(connection)
        connection.commit()
        if dry_run or not owed:
            return owed
        command.upgrade(alembic_config(connection=connection, passwords=passwords), "heads")
        connection.commit()
        return owed
    finally:
        connection.exec_driver_sql("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        connection.commit()


def passwords_for(config: Any) -> dict[str, str]:
    """§5.5's two generated passwords, as the revision's parameters."""
    return {"app_password": config.app_password, "read_password": config.read_password}


def migrate_instance(instance_dir: Path | str, *, dry_run: bool = False) -> tuple[str, ...]:
    """Bring one installation's configured board store to the schema this build ships.

    Returns the revisions applied, or — under ``dry_run`` — the revisions that *would* be applied,
    having connected and read but written nothing.

    Every failure leaves as one `BoardStoreError` carrying its reason: a `board-store.env` the
    instance repository tracks, an unparsable one, absent dependencies, a server that will not
    answer, a refused login and a revision that fails all reach the caller in the same shape.
    That is what lets `upgrade.py`'s step report a reason without importing SQLAlchemy — which
    matters, because the same upgrade that installs the dependencies has to start without them.
    """
    config = board_store.resolve(instance_dir)
    try:
        import sqlalchemy as sa
    except ImportError as exc:
        raise BoardStoreError(
            "the board store is configured but SQLAlchemy is not installed; reinstall the "
            "product dependencies before migrating"
        ) from exc
    try:
        import alembic  # noqa: F401
    except ImportError as exc:
        raise BoardStoreError(
            "the board store is configured but Alembic is not installed; reinstall the "
            "product dependencies before migrating"
        ) from exc
    engine = sa.create_engine(sqlalchemy_url(config.for_role("owner")))
    try:
        with engine.connect() as connection:
            return apply(connection, passwords=passwords_for(config), dry_run=dry_run)
    except sa.exc.SQLAlchemyError as exc:
        raise BoardStoreError(f"the board store did not accept the migration run: {exc}") from exc
    finally:
        engine.dispose()


__all__ = [
    "ADVISORY_LOCK_KEY",
    "EXPECTED_SCHEMA_REVISION",
    "SCRIPT_LOCATION",
    "alembic_config",
    "apply",
    "assert_schema_revision",
    "current_revision",
    "head_revision",
    "migrate_instance",
    "passwords_for",
    "pending",
    "script_directory",
    "sqlalchemy_url",
]
