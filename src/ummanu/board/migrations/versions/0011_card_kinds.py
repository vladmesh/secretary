"""Card kinds: `infra` joins the vocabulary; the review choice and the live-impact flag are columns.

`task_type`'s named CHECK from `0003` is restated with `infra`; NULL stays legal for the legacy
row that never named a type. `review` is nullable: every existing row was reviewed and carries no
stored choice, and readers take NULL as `required` rather than this revision inventing a value.
`live_impact` defaults to false, which is what every existing row is, and only a research card may
carry it (secretary-1638).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_card_kinds"
down_revision = "0010_po_session_close"
branch_labels = None
depends_on = None

TASK_TYPE_CHECK = "task_type_is_a_known_type_or_nothing"


def upgrade() -> None:
    op.drop_constraint(TASK_TYPE_CHECK, "tasks", type_="check")
    op.create_check_constraint(
        TASK_TYPE_CHECK, "tasks", "task_type IS NULL OR task_type IN ('code','research','infra')"
    )
    op.add_column("tasks", sa.Column("review", sa.Text()))
    op.add_column(
        "tasks", sa.Column("live_impact", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )
    op.create_check_constraint(
        "task_review_is_a_known_choice_or_nothing", "tasks", "review IS NULL OR review IN ('required','skipped')"
    )
    op.create_check_constraint(
        "task_live_impact_is_research_only", "tasks", "NOT live_impact OR task_type IS NOT DISTINCT FROM 'research'"
    )


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
