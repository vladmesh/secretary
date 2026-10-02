"""The last card the board holds and the store could not: `task_type` with no value.

`secretary-1585` imported the whole live board and reported one honest loss, out of 944 cards:

    MISSING card secretary-583: task_type '' is outside the CHECK vocabulary ['code', 'research']

`secretary-583` carries no `task_type` metadata at all.  This is the same finding `0002` closed
for `project_id` on the same card, and it is closed the same way: **the board's silence is stored
as NULL, not as an invented type.**  Substituting `code` would put a fact in the store that the
board does not state, and the product's own reader does not substitute one either —
`tasks._card` returns `_text(meta.get("task_type"))`, that is `''`, for exactly this card.

`tasks.task_type` therefore becomes nullable, and its `CHECK` is restated so it admits NULL *or*
a value of the vocabulary.  It is not widened into "any text": the vocabulary is still closed
(§3.12), and the only thing added is the absence of a value.  Nothing else in `0001` or `0002`
is touched, and the catalogue of §3.13 is unchanged by this revision — one `CHECK` is dropped and
one is created in its place.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_task_type_optional"
down_revision = "0002_board_gaps"
branch_labels = None
depends_on = None

#: PostgreSQL's own name for `0001`'s unnamed single-column `CHECK` on this column.  The
#: replacement is named, so the next revision that has to touch it does not have to know this.
GENERATED_NAME = "tasks_task_type_check"

#: The vocabulary, unchanged; only the absence of a value is added to what the column admits.
NAMED_CHECK = "task_type_is_a_known_type_or_nothing"
CHECK = "task_type IS NULL OR task_type IN ('code','research')"


def upgrade() -> None:
    # === §3.5 and §8.6: the board's silence about a card's type is a NULL. ===
    op.alter_column("tasks", "task_type", existing_type=sa.Text(), nullable=True)
    op.drop_constraint(GENERATED_NAME, "tasks", type_="check")
    op.create_check_constraint(NAMED_CHECK, "tasks", CHECK)


def downgrade() -> None:
    """There is none, for `0001_initial`'s reason (§7.4).

    A revision that fails rolls back whole, so the recovery is to fix the revision and run it
    again.  Unwinding this one would put back a `NOT NULL` that the card this revision exists for
    cannot satisfy.
    """
    raise NotImplementedError(
        "the board store has no down migration: a failed revision rolls back whole (§7.4), and "
        "unwinding this one would restore a NOT NULL that a card on the board cannot satisfy"
    )
