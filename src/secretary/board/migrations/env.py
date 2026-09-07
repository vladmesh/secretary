"""Alembic's environment for the board store.

The one thing this file does differently from a generated `env.py` is where the connection comes
from: **never** an `sqlalchemy.url` literal in an ini file.  §5.4 puts the connection of an
installation in `<instance>/board-store.env`, one credential per §5.5 role, and §7.4 runs
migrations as `secretary_owner`.

There is exactly **one** way in: the connection `secretary.board.migrate` opens from
`board_store.resolve` and injects as `config.attributes["connection"]`, with §5.5's two generated
passwords beside it in `config.attributes["passwords"]`.  The runner has to be the one that opens
it, because it holds `pg_advisory_lock` on that same session for the whole apply and a second
connection would not be holding anything.  An invocation that supplies no connection refuses here
and names that entry point, rather than opening a connection of its own and failing later inside
the revision on passwords nobody handed it.

`board_store.resolve` is also where the git-exclusion lifecycle is enforced (§5.4), so this route
cannot reach a configured store that is still tracked by the instance repository.
"""

from __future__ import annotations

from alembic import context

from secretary.board.schema import metadata as target_metadata

config = context.config

#: The refusal an unsupported invocation gets, named here so a test can pin it.
NO_CONNECTION = (
    "the board store's migrations run only through secretary.board.migrate, which opens the "
    "secretary_owner connection from board-store.env (§5.4) and holds the migration advisory "
    "lock on it (§7.4); this invocation supplied no connection"
)


def run_migrations() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "the board store's migrations are never rendered offline: §5.5's roles and grants "
            "take generated passwords that must not be written into a SQL script"
        )
    connection = config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(NO_CONNECTION)
    _run(connection)


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
