"""The origin-return outbox: one row per terminal transition of a delegated card (secretary-1792).

One new table, `origin_returns`, and nothing else: every existing row of every other table loads
unchanged. `SqlTaskAudit.append` writes a row in the transaction that commits a card's transition into
Done or Blocked when the card carries a PO origin and is not a wait card (`board/origin_outbox.py`);
the dispatcher reads the undelivered ones through the partial index and records each delivery once.
Transitions committed before this revision owe no row: no card carried an origin before this build.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from secretary.board.schema import APP_ROLE, READ_ROLE

revision = "0022_origin_returns"
down_revision = "0021_delegated_card_settled"
branch_labels = None
depends_on = None

_TIMESTAMPTZ = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "origin_returns",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("task_ref", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("target_state", sa.Text(), nullable=False),
        sa.Column("created_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("delivered_at", _TIMESTAMPTZ),
        sa.Column("status", sa.Text()),
        sa.Column("notice", sa.Text()),
        sa.Column("session", sa.Text()),
        sa.Column("po_request_id", sa.Text()),
        sa.CheckConstraint("target_state IN ('done','blocked')", name="origin_return_target_is_terminal"),
        sa.CheckConstraint(
            "status IS NULL OR status IN ('delivered','skipped')", name="origin_return_status_in_vocabulary"
        ),
        sa.CheckConstraint(
            "(delivered_at IS NULL) = (status IS NULL)", name="origin_return_status_with_its_delivery"
        ),
        sa.UniqueConstraint("event_id", name="origin_return_event_is_unique"),
    )
    op.create_index(
        "origin_returns_undelivered", "origin_returns", ["id"], postgresql_where=sa.text("delivered_at IS NULL")
    )
    op.create_index("origin_returns_by_card", "origin_returns", ["task_ref"])
    # The audit writes rows as the app role, and the dispatcher marks them (§5.5's app grants).
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON origin_returns TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON origin_returns TO {READ_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE origin_returns_id_seq TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("origin_returns")
