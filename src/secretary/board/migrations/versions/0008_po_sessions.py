"""PO head sessions, their turns and the feed (sprint:1442)."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from secretary.board.schema import APP_ROLE, READ_ROLE

revision = "0008_po_sessions"
down_revision = "0007_card_transport_key"
branch_labels = None
depends_on = None

_TIMESTAMPTZ = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "po_sessions",
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column("cli", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("cwd", sa.Text(), nullable=False),
        sa.Column("created_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("cli_session_id", sa.Text()),
        sa.CheckConstraint("cli IN ('claude','codex')", name="po_session_cli_in_vocabulary"),
        sa.CheckConstraint("state IN ('open','closed')", name="po_session_state_in_vocabulary"),
    )
    op.create_table(
        "po_turns",
        sa.Column(
            "session_id",
            sa.Text(),
            sa.ForeignKey("po_sessions.session_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("started_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("finished_at", _TIMESTAMPTZ),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("stdout_path", sa.Text(), nullable=False),
        sa.Column("pid", sa.Integer()),
        sa.Column("process_identity", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.CheckConstraint(
            "state IN ('running','completed','failed','interrupted')",
            name="po_turn_state_in_vocabulary",
        ),
        sa.CheckConstraint("seq > 0", name="po_turn_seq_is_positive"),
        sa.CheckConstraint(
            "(state = 'running') = (finished_at IS NULL)", name="po_turn_finished_unless_running"
        ),
    )
    # The invariant "at most one running turn per session" is the database's, not the runner's.
    op.create_index(
        "po_turns_one_running_per_session",
        "po_turns",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_table(
        "po_feed",
        sa.Column("entry_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", _TIMESTAMPTZ, nullable=False),
        sa.CheckConstraint("role IN ('owner','agent')", name="po_feed_role_in_vocabulary"),
        sa.ForeignKeyConstraint(
            ["session_id", "turn_seq"],
            ["po_turns.session_id", "po_turns.seq"],
            name="po_feed_entry_belongs_to_its_turn",
            ondelete="CASCADE",
        ),
    )
    op.create_index("po_feed_by_session", "po_feed", ["session_id", "entry_id"])

    for table in ("po_sessions", "po_turns", "po_feed"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"GRANT SELECT ON {table} TO {READ_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE po_feed_entry_id_seq TO {APP_ROLE}")


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
