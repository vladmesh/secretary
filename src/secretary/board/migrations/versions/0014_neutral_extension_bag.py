"""The extension bag's top-level key becomes neutral: current rows move onto `extra`.

`tasks`, `products` and `issues` keep every metadata key the model does not name in one bag under
one top-level key of `extensions` (docs/BOARD_STORE.md §8.2).  Until this revision that key was the
retired board's name.  This revision moves the bag of every current row onto `extra`, merging with a
bag already there, inside the migration's one transaction.

The revision does not spell the old key.  It relies on a premise instead: across the three tables
the only top-level key other than `extra` and the documented markers (`board_never_named`, §3.10,
which stay where they are) is the old one.  The premise is checked on the input, and
the revision refuses, naming what it found, when it does not hold — more than one other key, a
non-object `extensions` or bag, or a field both bags name with different values.  A store with no
other key has nothing to move.

The done-retention retry key's identity changes in the same release (`tasks.
_done_retention_request_id`), so a retirement staged under the old identity would be orphaned.  The
revision refuses a store holding any non-committed `done-retention-` request and names it.

History is not rewritten: `board_events`, committed `requests` and checkpoints keep what they
recorded.  A record written before this revision is read through `secretary.board.extension_bag.
fold_extension_bags`.

The downgrade is refused: board-store migrations are forward-only.
"""

from __future__ import annotations

from alembic import op

revision = "0014_neutral_extension_bag"
down_revision = "0013_budget_candidates"
branch_labels = None
depends_on = None

#: `secretary.board.extension_bag.EXTENSION_BAG` as it stood at this revision, copied rather than
#: imported so the revision never changes after it is applied.  A test compares the two.
NEW_KEY = "extra"

#: `secretary.board.extension_bag.EXTENSION_MARKERS` at this revision, copied for the same reason.
MARKERS = ("board_never_named",)

#: The rows this revision moves, and the column that names each one in a refusal.
TABLES = (("tasks", "task_ref"), ("products", "product_id"), ("issues", "issue_id"))

DONE_RETENTION_PREFIX = "done-retention-"


class ExtensionBagMigrationRefused(RuntimeError):
    """The input does not satisfy this revision's premise; nothing was changed."""


def _named(rows: list[tuple[str, ...]], limit: int = 10) -> str:
    shown = ", ".join(":".join(str(part) for part in row) for row in rows[:limit])
    return shown + (f" and {len(rows) - limit} more" if len(rows) > limit else "")


def _refuse_pending_done_retention(connection) -> None:
    rows = connection.exec_driver_sql(
        "SELECT request_id, status FROM requests WHERE request_id LIKE %(prefix)s "
        "AND status <> 'committed' ORDER BY request_id",
        {"prefix": DONE_RETENTION_PREFIX + "%"},
    ).fetchall()
    if rows:
        raise ExtensionBagMigrationRefused(
            "the done-retention retry identity changes in this release and would orphan "
            f"{len(rows)} non-committed request(s): {_named([tuple(row) for row in rows])}"
        )


def _other_key(connection) -> str | None:
    for table, column in TABLES:
        rows = connection.exec_driver_sql(
            f"SELECT {column} FROM {table} WHERE jsonb_typeof(extensions) <> 'object' "
            f"ORDER BY {column}"
        ).fetchall()
        if rows:
            raise ExtensionBagMigrationRefused(
                f"{table}.extensions is not an object on {_named([(table, row[0]) for row in rows])}"
            )
    union = " UNION ".join(
        f"SELECT jsonb_object_keys(extensions) AS key FROM {table}" for table, _ in TABLES
    )
    keys = [
        row[0]
        for row in connection.exec_driver_sql(
            f"SELECT DISTINCT key FROM ({union}) AS keys "
            f"WHERE key <> %(new)s AND NOT key = ANY(%(markers)s) ORDER BY key",
            {"new": NEW_KEY, "markers": list(MARKERS)},
        ).fetchall()
    ]
    if len(keys) > 1:
        raise ExtensionBagMigrationRefused(
            f"expected at most one top-level extensions key besides {NEW_KEY!r} and the markers "
            f"{list(MARKERS)!r}, found {keys!r}"
        )
    return keys[0] if keys else None


def _refuse_unmergeable(connection, old: str) -> None:
    params = {"old": old, "new": NEW_KEY}
    for table, column in TABLES:
        rows = connection.exec_driver_sql(
            f"SELECT {column} FROM {table} WHERE extensions ? %(old)s AND ("
            f"jsonb_typeof(extensions -> %(old)s) <> 'object' OR (extensions ? %(new)s "
            f"AND jsonb_typeof(extensions -> %(new)s) <> 'object')) ORDER BY {column}",
            params,
        ).fetchall()
        if rows:
            raise ExtensionBagMigrationRefused(
                f"the {table} bag is not an object on {_named([(table, row[0]) for row in rows])}"
            )
        rows = connection.exec_driver_sql(
            f"SELECT {column}, field.key FROM {table}, jsonb_each(extensions -> %(old)s) AS field "
            f"WHERE extensions ? %(old)s AND extensions -> %(new)s ? field.key "
            f"AND extensions -> %(new)s -> field.key <> field.value ORDER BY {column}, field.key",
            params,
        ).fetchall()
        if rows:
            raise ExtensionBagMigrationRefused(
                f"both bags name a field with different values on "
                f"{_named([(table, row[0], row[1]) for row in rows])}"
            )


def upgrade() -> None:
    connection = op.get_bind()
    _refuse_pending_done_retention(connection)
    old = _other_key(connection)
    if old is None:
        return
    _refuse_unmergeable(connection, old)
    for table, _column in TABLES:
        connection.exec_driver_sql(
            f"UPDATE {table} SET extensions = (extensions - %(old)s) || jsonb_build_object("
            f"%(new)s::text, (extensions -> %(old)s) || coalesce(extensions -> %(new)s, '{{}}'::jsonb)) "
            f"WHERE extensions ? %(old)s",
            {"old": old, "new": NEW_KEY},
        )


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
