"""Sprints: the PO session that opened a sprint, and the productions its operations may touch.

Also `po_sprint_session`, the PO service's resolver opening a session for a sprint, joins the
operations a `po_requests` row may record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "0016_sprint_po_session"
down_revision = "0015_po_effort_resolved_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every existing sprint was opened before either was recorded: no PO session, and no production
    # its operations may touch. Nothing is inferred for them. The table-level grants on `sprints`
    # cover new columns.
    op.add_column("sprints", sa.Column("po_session", sa.Text(), nullable=True))
    op.add_column(
        "sprints",
        sa.Column(
            "allowed_productions",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    # Spelled here, not imported: a revision is frozen at what it was when it shipped.
    # `po_request_seq_only_for_a_send` is unchanged: a sprint-session request records no turn.
    op.drop_constraint("po_request_operation_in_vocabulary", "po_requests", type_="check")
    op.create_check_constraint(
        "po_request_operation_in_vocabulary",
        "po_requests",
        "operation IN ('po_session_create','po_send','po_sprint_session')",
    )


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
