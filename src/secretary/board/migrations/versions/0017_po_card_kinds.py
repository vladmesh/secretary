"""Card kinds `decision` and `operation`: cards the PO service executes instead of a head.

`task_type`'s named CHECK is restated with both; NULL stays legal for the legacy row that never
named a type, and every existing row of `code`, `research` or `infra` loads unchanged. Neither kind
adds a column: its review choice is `skipped`, stored in the `review` column from `0011`.
"""

from __future__ import annotations

from alembic import op

revision = "0017_po_card_kinds"
down_revision = "0016_sprint_po_session"
branch_labels = None
depends_on = None

TASK_TYPE_CHECK = "task_type_is_a_known_type_or_nothing"


def upgrade() -> None:
    # Spelled here, not imported: a revision is frozen at what it was when it shipped.
    op.drop_constraint(TASK_TYPE_CHECK, "tasks", type_="check")
    op.create_check_constraint(
        TASK_TYPE_CHECK,
        "tasks",
        "task_type IS NULL OR task_type IN ('code','research','infra','decision','operation')",
    )


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
