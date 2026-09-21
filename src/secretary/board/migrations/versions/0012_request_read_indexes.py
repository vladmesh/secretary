"""Audit reads narrow in SQL: indexes on `requests` for every filter `SqlTaskAudit` applies.

Indexes only, on existing columns and JSON expressions: nothing is added to a row and nothing is
rewritten. The code that uses them is correct without them, so the dispatcher may run it before
this revision is applied; what the revision changes is the plan, from a sequential scan and a sort
of the whole history to an index read of the slice asked for (secretary-1658).

* `requests_committed_by_ref`: one ref or a set of refs, already in claim order.
* `requests_committed_in_claim_order`: a window by settle time, a page and the committed count.
* `requests_staged_in_claim_order`: the staged set and its count.
* `requests_by_kind`: a kind narrowing, and the occurrence projections' slice of one kind.
* `requests_by_event_id`: `event_id_owner` and the projections' shared-event check.
* `requests_owing_outcome`: the records outcome recovery still has to look at.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_request_read_indexes"
down_revision = "0011_card_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "requests_committed_by_ref",
        "requests",
        ["ref", "settled_at", "created_at", "request_id"],
        postgresql_where=sa.text("status = 'committed'"),
    )
    op.create_index(
        "requests_committed_in_claim_order",
        "requests",
        ["settled_at", "created_at", "request_id"],
        postgresql_where=sa.text("status = 'committed'"),
    )
    op.create_index(
        "requests_staged_in_claim_order",
        "requests",
        ["created_at", "request_id"],
        postgresql_where=sa.text("status = 'staged'"),
    )
    op.create_index("requests_by_kind", "requests", [sa.text("(intent ->> 'kind')"), "status"])
    op.create_index(
        "requests_by_event_id", "requests", [sa.text("(intent ->> 'event_id')"), "created_at", "request_id"]
    )
    op.create_index(
        "requests_owing_outcome",
        "requests",
        ["request_id"],
        postgresql_where=sa.text("(intent -> 'data') ? 'attempt_outcome_owed'"),
    )


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
