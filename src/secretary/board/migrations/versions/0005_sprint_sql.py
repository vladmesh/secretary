"""Preserve the declared order of normalized Sprint relations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from secretary.board.schema import APP_ROLE, READ_ROLE

revision = "0005_sprint_sql"
down_revision = "0004_product_issue_sql"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("sprint_repositories", "sprint_issues", "sprint_projects"):
        op.add_column(table, sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"))
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
        op.execute(f"GRANT SELECT ON {table} TO {READ_ROLE}")
    op.add_column("sprint_resumes", sa.Column("recorded_at_source", sa.Text(), nullable=True))
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON sprint_resumes TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON sprint_resumes TO {READ_ROLE}")
    # A restore replays every exported occurrence under its one calling request.
    op.drop_constraint("sprint_budget_events_request_id_key", "sprint_budget_events", type_="unique")
    op.drop_constraint("tasks_sprint_ref_fkey", "tasks", type_="foreignkey")
    op.create_foreign_key(
        "tasks_sprint_ref_fkey",
        "tasks",
        "sprints",
        ["sprint_ref"],
        ["ref"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
