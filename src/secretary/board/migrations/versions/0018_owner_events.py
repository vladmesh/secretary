"""Owner events: what needs the owner and what the owner should know, behind the web's bell (secretary-1770).

One new table and nothing else: every existing row of every other table loads unchanged. The kinds,
the two classes and the rule that derives a class from its kind are CHECKs, spelled here as they
shipped (`secretary.board.owner_events.KIND_CLASS` is the same list today).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from secretary.board.schema import APP_ROLE, READ_ROLE

revision = "0018_owner_events"
down_revision = "0017_po_card_kinds"
branch_labels = None
depends_on = None

_TIMESTAMPTZ = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "owner_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("class", sa.Text(), nullable=False),
        sa.Column("subject_ref", sa.Text()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", _TIMESTAMPTZ, nullable=False),
        sa.Column("read_at", _TIMESTAMPTZ),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
            "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red')",
            name="owner_event_kind_in_vocabulary",
        ),
        sa.CheckConstraint("class IN ('needs_owner','notice')", name="owner_event_class_in_vocabulary"),
        sa.CheckConstraint(
            "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human'))",
            name="owner_event_class_follows_kind",
        ),
        sa.UniqueConstraint("dedup_key", name="owner_event_dedup_key_is_unique"),
    )
    op.create_index("owner_events_by_subject", "owner_events", ["subject_ref"])
    # Every producer writes and the web marks events read as the app role (§5.5's app grants).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON owner_events TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON owner_events TO {READ_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE owner_events_id_seq TO {APP_ROLE}")


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
