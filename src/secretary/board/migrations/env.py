"""Alembic's environment for the board store.

The one thing this file does differently from a generated `env.py` is where the connection comes
from: **never** an `sqlalchemy.url` literal in an ini file.  §5.4 puts the connection of an
installation in `<instance>/board-store.env`, one credential per §5.5 role, and §7.4 runs
migrations as `secretary_owner`.  So the URL is `board_store.resolve`'s, and this file has two
ways of getting there and no third:

* the runner (`secretary.board.migrate`) opens the owner connection itself — it has to, because
  it holds `pg_advisory_lock` on that same session for the whole run — and hands it over in
  `config.attributes["connection"]`;
* an operator running `alembic` by hand passes the installation instead
  (`-x instance=/home/dev/secretary-instance`), and this file resolves the owner credential from
  that installation's `board-store.env`.

`board_store.resolve` is also where the git-exclusion lifecycle is enforced (§5.4), so neither
route can reach a configured store that is still tracked by the instance repository.
"""

from __future__ import annotations

from alembic import context

from secretary.board import store as board_store
from secretary.board.migrate import sqlalchemy_url
from secretary.board.schema import metadata as target_metadata

config = context.config


def _connectable():
    """An owner `Engine` for the installation named on the command line."""
    import sqlalchemy as sa

    instance = config.get_main_option("instance_dir") or context.get_x_argument(as_dictionary=True).get(
        "instance"
    )
    if not instance:
        raise RuntimeError(
            "no connection was supplied and no installation was named; run the migrations through "
            "secretary.board.migrate, or pass -x instance=<instance directory>"
        )
    credentials = board_store.resolve_role(instance, "owner")
    return sa.create_engine(sqlalchemy_url(credentials))


def run_migrations() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "the board store's migrations are never rendered offline: §5.5's roles and grants "
            "take generated passwords that must not be written into a SQL script"
        )
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = _connectable()
    try:
        with engine.begin() as opened:
            _run(opened)
    finally:
        engine.dispose()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transaction_per_migration=True,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
