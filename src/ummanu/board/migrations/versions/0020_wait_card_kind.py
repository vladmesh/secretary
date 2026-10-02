"""Card kind `wait`: a durable, headless wait the dispatcher advances (secretary-1790).

`task_type`'s named CHECK is restated with `wait`; NULL stays legal for the legacy row that never
named a type, and every existing row loads unchanged. The kind adds no column: its spec and the
dispatcher's state are typed fields of the extension bag (`wait`, `wait_state`, `wait_cancel`).
The downgrade restores the 0017 constraint, and refuses while a `wait` card exists.
"""

from __future__ import annotations

from alembic import op

revision = "0020_wait_card_kind"
down_revision = "0019_po_session_title"
branch_labels = None
depends_on = None

TASK_TYPE_CHECK = "task_type_is_a_known_type_or_nothing"


def upgrade() -> None:
    # Spelled here, not imported: a revision is frozen at what it was when it shipped.
    op.drop_constraint(TASK_TYPE_CHECK, "tasks", type_="check")
    op.create_check_constraint(
        TASK_TYPE_CHECK,
        "tasks",
        "task_type IS NULL OR task_type IN ('code','research','infra','decision','operation','wait')",
    )


def downgrade() -> None:
    op.drop_constraint(TASK_TYPE_CHECK, "tasks", type_="check")
    op.create_check_constraint(
        TASK_TYPE_CHECK,
        "tasks",
        "task_type IS NULL OR task_type IN ('code','research','infra','decision','operation')",
    )
