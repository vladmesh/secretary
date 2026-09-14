"""PO session close: who closed a session and when, on the session row (secretary-1637)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_po_session_close"
down_revision = "0009_po_requests"
branch_labels = None
depends_on = None

_TIMESTAMPTZ = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    # The audit record of the owner's close lives on the row; every existing row is open, so both
    # columns are null and the CHECK holds for it. Grants on `po_sessions` from 0008 cover new columns.
    op.add_column("po_sessions", sa.Column("closed_at", _TIMESTAMPTZ))
    op.add_column("po_sessions", sa.Column("closed_by", sa.Text()))
    op.create_check_constraint(
        "po_session_closed_iff_audited",
        "po_sessions",
        "(state = 'closed') = (closed_at IS NOT NULL) AND (state = 'closed') = (closed_by IS NOT NULL)",
    )


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
