"""Owner event kind `e2e_after_merge`: an after-merge e2e run needs the owner (secretary-1807).

`owner_event_kind_in_vocabulary` and `owner_event_class_follows_kind` are restated with `e2e_after_merge`,
a `needs_owner` kind: a project's after-merge e2e run ended with nothing the pipeline can act on by itself
(cancelled, timed out, its deadline passed, its run unreadable or refused), or went red with no open sprint
and no PO origin to own the hotfix card. Every existing event loads unchanged, and nothing else changes: the
after-merge records are typed fields of a card's `e2e` bag field, no column. The downgrade restores the 0023
constraints, and refuses while an `e2e_after_merge` event exists.
"""

from __future__ import annotations

from alembic import op

revision = "0024_e2e_after_merge_kind"
down_revision = "0023_sprint_e2e_budget"
branch_labels = None
depends_on = None

KIND_CHECK = "owner_event_kind_in_vocabulary"
CLASS_CHECK = "owner_event_class_follows_kind"


def upgrade() -> None:
    # Spelled here, not imported: a revision is frozen at what it was when it shipped.
    op.drop_constraint(KIND_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        KIND_CHECK,
        "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled','e2e_budget_spent','e2e_after_merge')",
    )
    op.drop_constraint(CLASS_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        CLASS_CHECK,
        "owner_events",
        "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human','e2e_budget_spent',"
        "'e2e_after_merge'))",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.exec_driver_sql("SELECT 1 FROM owner_events WHERE kind = 'e2e_after_merge' LIMIT 1").first():
        raise RuntimeError("an e2e_after_merge owner event exists; 0024 cannot be downgraded")
    op.drop_constraint(CLASS_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        CLASS_CHECK,
        "owner_events",
        "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human','e2e_budget_spent'))",
    )
    op.drop_constraint(KIND_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        KIND_CHECK,
        "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled','e2e_budget_spent')",
    )
