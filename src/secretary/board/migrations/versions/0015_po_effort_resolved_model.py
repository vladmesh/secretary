"""PO sessions: the reasoning effort a session was opened with, and the model each turn resolved to."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_po_effort_resolved_model"
down_revision = "0014_neutral_extension_bag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every existing session was opened before an effort could be chosen, so it ran with the CLI's
    # own: `default`, the value that passes no flag. Grants on both tables from 0008 cover new columns.
    op.add_column(
        "po_sessions", sa.Column("effort", sa.Text(), nullable=False, server_default=sa.text("'default'"))
    )
    # What the CLI reported it ran for one turn; null for a turn that reported nothing, and for every
    # turn settled before this revision.
    op.add_column("po_turns", sa.Column("resolved_model", sa.Text()))


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
