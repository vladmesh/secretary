"""A real card decision/PO wait can publish its own needs_owner event.

Only the vocabulary constraints widen. Previous rows, columns and kind/class meanings
remain unchanged; the previous runtime reads unknown kinds as ordinary OwnerEvent rows.
The releasing runtime already supports this additive declaration and these Alembic APIs.
"""

from alembic import op

revision = "0025_card_waits_for_person"
down_revision = "0024_e2e_after_merge_kind"
branch_labels = None
depends_on = None
release_safety = "additive"


def upgrade() -> None:
    op.drop_constraint("owner_event_kind_in_vocabulary", "owner_events", type_="check")
    op.create_check_constraint(
        "owner_event_kind_in_vocabulary", "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled','e2e_budget_spent','e2e_after_merge','card_waits_for_person')",
    )
    op.drop_constraint("owner_event_class_follows_kind", "owner_events", type_="check")
    op.create_check_constraint(
        "owner_event_class_follows_kind", "owner_events",
        "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human','e2e_budget_spent',"
        "'e2e_after_merge','card_waits_for_person'))",
    )
    # Existing real waits also need a bell after the upgrade. Backfill only open,
    # unresolved sprint cards, with no already open needs_owner event; no GET writes.
    op.get_bind().exec_driver_sql(
        "INSERT INTO owner_events (kind, class, subject_ref, text, created_at, dedup_key) "
        "SELECT 'card_waits_for_person', 'needs_owner', t.task_ref, "
        "t.task_ref || ': existing sprint card awaits a person', now(), "
        "'card_waits_for_person:' || t.task_ref || ':0025' "
        "FROM tasks t JOIN sprints s ON s.ref = t.sprint_ref AND s.status = 'open' "
        "WHERE NOT t.archived AND ((t.state = 'blocked' AND t.task_type <> 'wait') OR "
        "(t.state = 'in_progress' AND t.task_type IN ('decision','operation'))) "
        "AND NOT EXISTS (SELECT 1 FROM task_supersessions u WHERE u.supersedes = t.task_ref) "
        "AND NOT EXISTS (SELECT 1 FROM owner_events e WHERE e.subject_ref = t.task_ref "
        "AND e.class = 'needs_owner' AND e.read_at IS NULL) "
        "ON CONFLICT (dedup_key) DO NOTHING"
    )


def downgrade() -> None:
    if op.get_bind().exec_driver_sql("SELECT 1 FROM owner_events WHERE kind = 'card_waits_for_person' LIMIT 1").first():
        raise RuntimeError("a card_waits_for_person event exists; 0025 cannot be downgraded")
    op.drop_constraint("owner_event_class_follows_kind", "owner_events", type_="check")
    op.create_check_constraint(
        "owner_event_class_follows_kind", "owner_events",
        "(class = 'needs_owner') = (kind IN ('card_handed_to_owner','steward_needs_human','e2e_budget_spent',"
        "'e2e_after_merge'))",
    )
    op.drop_constraint("owner_event_kind_in_vocabulary", "owner_events", type_="check")
    op.create_check_constraint(
        "owner_event_kind_in_vocabulary", "owner_events",
        "kind IN ('card_handed_to_owner','steward_needs_human','sprint_closed','sprint_stopped',"
        "'budget_signal','observer_dead','head_dead','po_turn_failed','provider_red',"
        "'delegated_card_settled','e2e_budget_spent','e2e_after_merge')",
    )
