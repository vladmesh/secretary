"""A sprint's e2e run budget, and the bell kind of an e2e budget nobody can raise (secretary-1796).

Two columns on `sprints`: `e2e_budget` (runs the sprint may dispatch, default 3) and `e2e_used` (runs it
dispatched), with one CHECK that neither is negative. The server defaults give every existing sprint,
open ones included, a budget of 3 and nothing used. The table-level grants on `sprints` cover them.
One new table, `sprint_e2e_charges`: one row per charged run (its dispatch id is the key, the card, the
sprint, when), written with the increment in the transaction of the run's intent.

`owner_event_kind_in_vocabulary` and `owner_event_class_follows_kind` are restated with
`e2e_budget_spent`, a `needs_owner` kind: a card outside every sprint, with no PO origin, whose per-card
e2e cap is spent. The downgrade restores the 0021 constraints and drops the table and the columns; it refuses while an
`e2e_budget_spent` event exists.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ummanu.board.schema import APP_ROLE, READ_ROLE

revision = "0023_sprint_e2e_budget"
down_revision = "0022_origin_returns"
branch_labels = None
depends_on = None

KIND_CHECK = "owner_event_kind_in_vocabulary"
CLASS_CHECK = "owner_event_class_follows_kind"
BUDGET_CHECK = "sprint_e2e_counts_are_not_negative"


def upgrade() -> None:
    op.add_column("sprints", sa.Column("e2e_budget", sa.Integer(), nullable=False, server_default=sa.text("3")))
    op.add_column("sprints", sa.Column("e2e_used", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.create_check_constraint(BUDGET_CHECK, "sprints", "e2e_budget >= 0 AND e2e_used >= 0")
    op.create_table(
        "sprint_e2e_charges",
        sa.Column("dispatch_id", sa.Text(), primary_key=True),
        sa.Column("sprint_ref", sa.Text(), sa.ForeignKey("sprints.ref", ondelete="CASCADE"), nullable=False),
        sa.Column("task_ref", sa.Text(), nullable=False),
        sa.Column("charged_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("sprint_e2e_charges_by_sprint", "sprint_e2e_charges", ["sprint_ref"])
    # The dispatcher charges as the app role; nothing updates or deletes a charge.
    op.execute(f"GRANT SELECT, INSERT ON sprint_e2e_charges TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON sprint_e2e_charges TO {READ_ROLE}")
    # Spelled here, not imported: a revision is frozen at what it was when it shipped.
    op.drop_constraint(KIND_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        KIND_CHECK,
        "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled','e2e_budget_spent')",
    )
    op.drop_constraint(CLASS_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        CLASS_CHECK,
        "owner_events",
        "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human','e2e_budget_spent'))",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql("SELECT 1 FROM owner_events WHERE kind = 'e2e_budget_spent' LIMIT 1").first():
        raise RuntimeError("an e2e_budget_spent owner event exists; 0023 cannot be downgraded")
    op.drop_constraint(CLASS_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        CLASS_CHECK,
        "owner_events",
        "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human'))",
    )
    op.drop_constraint(KIND_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        KIND_CHECK,
        "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled')",
    )
    op.drop_table("sprint_e2e_charges")
    op.drop_constraint(BUDGET_CHECK, "sprints", type_="check")
    op.drop_column("sprints", "e2e_used")
    op.drop_column("sprints", "e2e_budget")
