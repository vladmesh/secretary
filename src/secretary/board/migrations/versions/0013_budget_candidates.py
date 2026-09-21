"""The budget pass's candidate set: one partial index on `requests` holding only candidate rows.

An index only, on existing columns and JSON expressions: nothing is added to a row and nothing is
rewritten. The budget pass reads the oldest uncharged candidates (`SqlTaskAudit.
uncharged_budget_candidates`) with the predicate this index is built on, so the read walks this index
in claim order and probes each candidate's charge id through the primary key, instead of reading every
committed record (secretary-1661). The code is correct without it; only the plan differs.

The predicate is `secretary.board.budget_candidates.CANDIDATE_PREDICATE` as it stood at this revision,
copied rather than imported so the revision never changes after it is applied. A test compares the
two, because the planner uses the index only while the query's predicate implies this one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_budget_candidates"
down_revision = "0012_request_read_indexes"
branch_labels = None
depends_on = None

#: The candidate predicate over `intent`, verbatim.
CANDIDATE_PREDICATE = (
    "(intent ->> 'ref') <> '' AND (intent ->> 'ref') NOT LIKE 'sprint:%' AND ("
    "((intent ->> 'kind') IN ('verdict', 'card.verdict') "
    "AND 'review:red' IN ((intent -> 'payload' ->> 'marker'), (intent -> 'data' ->> 'marker')))"
    " OR (intent -> 'transition' ->> 'target') = 'blocked'"
    " OR ((intent ->> 'kind') = 'moved' AND (intent -> 'payload' ->> 'to') = 'blocked')"
    " OR ((intent -> 'transition' ->> 'target') = 'ready'"
    " AND (intent -> 'transition' ->> 'source') IN ('assessment', 'in_progress', 'validate'))"
    " OR ((intent ->> 'kind') = 'moved' AND (intent -> 'payload' ->> 'to') = 'ready'"
    " AND (intent -> 'payload' ->> 'from') IN ('assessment', 'in_progress', 'validate'))"
    " OR (((intent -> 'transition' ->> 'target') = 'in_progress'"
    " OR ((intent ->> 'kind') = 'moved' AND (intent -> 'payload' ->> 'to') = 'in_progress'))"
    " AND (intent ->> 'request_id') LIKE '%gate-red%')"
    " OR ((intent ->> 'kind') = 'created'"
    " AND ((intent -> 'payload' ->> 'budget_event') IN ('hotfix', 'recreated_task')"
    " OR (intent -> 'data' ->> 'budget_event') IN ('hotfix', 'recreated_task'))))"
)


def upgrade() -> None:
    op.create_index(
        "requests_budget_candidates",
        "requests",
        ["settled_at", "created_at", "request_id"],
        postgresql_where=sa.text("status = 'committed' AND " + CANDIDATE_PREDICATE),
    )


def downgrade() -> None:
    raise NotImplementedError("board-store migrations are forward-only")
