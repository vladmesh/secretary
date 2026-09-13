"""The form request id a PO turn was started under: one request id, at most one turn (secretary-1631)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_po_turn_request_id"
down_revision = "0008_po_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: a turn started outside a form (the runner's own callers, tests) carries none, and
    # PostgreSQL treats NULLs as distinct under the unique index.
    op.add_column("po_turns", sa.Column("request_id", sa.Text()))
    op.create_index(
        "po_turns_one_turn_per_request",
        "po_turns",
        ["session_id", "request_id"],
        unique=True,
    )


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
