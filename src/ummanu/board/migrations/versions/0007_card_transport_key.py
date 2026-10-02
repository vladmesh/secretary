"""Give Card rows their collision-free board-client transport key."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_card_transport_key"
down_revision = "0006_sprint_transport_key"
branch_labels = None
depends_on = None

_CARD_KEY_LIMIT = 2_000_000_000


def upgrade() -> None:
    op.execute(
        sa.text(
            "CREATE SEQUENCE card_board_key_seq AS bigint START WITH 1 "
            "MINVALUE 1 MAXVALUE 1999999999 NO CYCLE"
        )
    )
    op.add_column(
        "tasks",
        sa.Column(
            "board_key",
            sa.BigInteger(),
            server_default=sa.text("nextval('card_board_key_seq'::regclass)"),
            nullable=True,
        ),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "WITH numbered AS ("
            " SELECT task_ref, row_number() OVER (ORDER BY task_ref) AS board_key FROM tasks"
            ") UPDATE tasks SET board_key = numbered.board_key "
            "FROM numbered WHERE tasks.task_ref = numbered.task_ref"
        )
    )
    count = int(connection.execute(sa.text("SELECT count(*) FROM tasks")).scalar_one())
    if count >= _CARD_KEY_LIMIT:
        raise RuntimeError("the existing Card population exhausts the transport-key range")
    connection.execute(
        sa.text("SELECT setval('card_board_key_seq', :value, :called)"),
        {"value": max(count, 1), "called": count > 0},
    )
    # The volatile default rewrote `tasks` in this transaction, so every row the backfill just
    # updated counts as inserted here, and PostgreSQL then queues the INITIALLY DEFERRED
    # `tasks.sprint_ref` check of each card inside a sprint for commit time even though the key
    # did not change. ALTER TABLE refuses a relation with pending trigger events, so settle them
    # first; a store whose deferred constraints cannot be satisfied is not one this migration
    # should silently carry.
    connection.execute(sa.text("SET CONSTRAINTS ALL IMMEDIATE"))
    op.alter_column(
        "tasks",
        "board_key",
        nullable=False,
    )
    op.create_check_constraint(
        "task_board_key_is_in_card_range",
        "tasks",
        "board_key > 0 AND board_key < 2000000000",
    )
    op.create_unique_constraint("uq_tasks_board_key", "tasks", ["board_key"])


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
