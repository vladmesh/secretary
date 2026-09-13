"""PO request ids: each /po form request id owns one operation with fixed inputs (secretary-1631)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from secretary.board.schema import APP_ROLE, READ_ROLE

revision = "0009_po_requests"
down_revision = "0008_po_sessions"
branch_labels = None
depends_on = None

_TIMESTAMPTZ = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    # The one idempotency record of /po: written by `PoStore` in the transaction that creates the
    # session or the turn the request made, and read before anything else in that transaction.
    op.create_table(
        "po_requests",
        sa.Column("request_id", sa.Text(), primary_key=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("po_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer()),
        sa.Column("created_at", _TIMESTAMPTZ, nullable=False),
        sa.CheckConstraint(
            "operation IN ('po_session_create','po_send')", name="po_request_operation_in_vocabulary"
        ),
        sa.CheckConstraint(
            "(operation = 'po_send') = (seq IS NOT NULL)", name="po_request_seq_only_for_a_send"
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "seq"],
            ["po_turns.session_id", "po_turns.seq"],
            name="po_request_names_its_turn",
            ondelete="CASCADE",
        ),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON po_requests TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON po_requests TO {READ_ROLE}")


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
