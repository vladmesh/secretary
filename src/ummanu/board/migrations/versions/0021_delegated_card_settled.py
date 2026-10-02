"""Owner event kind `delegated_card_settled`: a delegated card's result went back to its PO session (secretary-1792).

`owner_event_kind_in_vocabulary` is restated with the new kind, a notice; the class rule
(`owner_event_class_follows_kind`) is unchanged, since only the two `needs_owner` kinds are named there.
Every existing event loads unchanged, and nothing else changes: a card's origin and its return state are
typed fields of the extension bag (`po_origin`, `po_return`), no column. The downgrade restores the 0018
constraint, and refuses while a `delegated_card_settled` event exists.
"""

from __future__ import annotations

from alembic import op

revision = "0021_delegated_card_settled"
down_revision = "0020_wait_card_kind"
branch_labels = None
depends_on = None

KIND_CHECK = "owner_event_kind_in_vocabulary"


def upgrade() -> None:
    # Spelled here, not imported: a revision is frozen at what it was when it shipped.
    op.drop_constraint(KIND_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        KIND_CHECK,
        "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled')",
    )


def downgrade() -> None:
    op.drop_constraint(KIND_CHECK, "owner_events", type_="check")
    op.create_check_constraint(
        KIND_CHECK,
        "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red')",
    )
