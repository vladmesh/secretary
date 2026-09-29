"""Creation-only local-run exceptions, empty for every existing sprint.

A defaulted column and its shape constraint keep the previous release working.
Entry validation belongs to the sprint writer; packet reads also validate it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026_sprint_local_runs"
down_revision = "0025_card_waits_for_person"
branch_labels = None
depends_on = None
release_safety = "additive"


def upgrade() -> None:
    op.add_column(
        "sprints",
        sa.Column("local_run_exceptions", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_check_constraint(
        "sprint_local_runs_are_array", "sprints", "jsonb_typeof(local_run_exceptions) = 'array'"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .exec_driver_sql("SELECT 1 FROM sprints WHERE local_run_exceptions <> '[]'::jsonb LIMIT 1")
        .first()
    ):
        raise RuntimeError("a sprint declares local-run authority; 0026 cannot be downgraded")
    op.drop_constraint("sprint_local_runs_are_array", "sprints", type_="check")
    op.drop_column("sprints", "local_run_exceptions")
