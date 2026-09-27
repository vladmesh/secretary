"""PO sessions: a title the owner or the PO sets, and each sprint's session titled by its sprint (secretary-1782).

One nullable column: every existing session loads unchanged, untitled. The backfill is safe to run
once and harmless to repeat: it touches only a session some `sprints.po_session` recorded (0016), and
only where its title is still null, so it overwrites nothing a person chose. A session that opened
two sprints (two `sprint create` in one PO session) takes the first of them, by creation and then
ref, so the result does not depend on the scan order.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_po_session_title"
down_revision = "0018_owner_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Grants on `po_sessions` from 0008 cover the new column.
    op.add_column("po_sessions", sa.Column("title", sa.Text(), nullable=True))
    op.execute(
        "UPDATE po_sessions s SET title = ("
        "SELECT sp.ref FROM sprints sp WHERE sp.po_session = s.session_id "
        "ORDER BY sp.created_at, sp.ref LIMIT 1"
        ") WHERE s.title IS NULL "
        "AND EXISTS (SELECT 1 FROM sprints sp WHERE sp.po_session = s.session_id)"
    )


def downgrade() -> None:
    # Only a column and the titles in it: dropping it restores 0018's `po_sessions` exactly.
    op.drop_column("po_sessions", "title")
