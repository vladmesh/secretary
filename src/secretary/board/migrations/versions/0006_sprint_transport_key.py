"""Give Sprint rows their disjoint, indexed board-client transport key."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from secretary.board.backend import record_key

revision = "0006_sprint_transport_key"
down_revision = "0005_sprint_sql"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "task_number_is_in_card_key_range", "tasks", "task_number < 2000000000"
    )
    op.add_column("sprints", sa.Column("board_key", sa.BigInteger(), nullable=True))
    connection = op.get_bind()
    for reference, in connection.execute(sa.text("SELECT ref FROM sprints")):
        connection.execute(
            sa.text("UPDATE sprints SET board_key=:key WHERE ref=:ref"),
            {"key": record_key("sprint", str(reference)), "ref": reference},
        )
    op.alter_column("sprints", "board_key", nullable=False)
    op.create_unique_constraint("uq_sprints_board_key", "sprints", ["board_key"])


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
