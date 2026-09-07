"""The gaps the first import of real data found, closed in the schema.

`secretary-1583` ran the importer against today's whole board — 1513 Pipeline rows with 15 571
comments and 102 sprint rows — and named every record the `0001_initial` schema could not carry.
This revision is that list, and nothing else:

* **`issue_comments`** — 479 comments live on Issue rows and §3.7 declared two comment tables,
  both foreign-keyed to their own entity.  A re-read of the live board on 2026-09-07 counted the
  Product side too: **0** comments on the 8 Product rows, so there is no `product_comments` table
  and §3.7 records the counted zero instead of a third table nothing would fill.
* **`issues.extensions`** — nine leftover metadata keys ride on 158 Issue rows, and 72 Issues sit
  in a lane that is not their product's.  A card keeps that provenance in `tasks.extensions`
  (§8.2); an Issue had nowhere to keep it.
* **the sprint's identity** — `sprints.sprint_number integer PRIMARY KEY` cannot hold
  `sprint:canary-terra-20260813` or `sprint:canary-terra-final-20260813`, both live rows on the
  board, and `secretary-1438` / `secretary-1439` lost their sprint link with them.  §9's stable
  reference becomes the identity: `sprints.ref` is the primary key, `sprint_number` is a nullable
  unique column, `sprint_number_seq` still allocates numbers for numbered sprints, and every
  composite key that carried `sprint_number` now carries `ref` — the same scope, spelled with the
  identity that can hold every reference the board has.
* **two records with no representable field** — `secretary-583` carries no `project` metadata
  against a `NOT NULL` `tasks.project_id`, and nine `blocked_by` values name cards that are not
  on the board against a foreign key into `tasks`.  `project_id` becomes nullable and
  `task_dependencies` splits the reference it always keeps from the foreign key it can only
  sometimes resolve.

It is a revision *on top of* `0001_initial`, not a rewrite of it: an installation that ran the
initial revision arrives here by `ALTER TABLE`, which is why the sprint identity is converted
column by column with a backfill rather than by recreating the tables.  §5.5's grants are not
repeated — `0001` left `ALTER DEFAULT PRIVILEGES` behind for exactly this, so `issue_comments`
is reachable by `secretary_app` and `secretary_read` without a further grant.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_board_gaps"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

ISSUE_REF = sa.Computed("'issue:' || issue_id", persisted=True)

#: The tables whose scoping column was `sprint_number` and is now the sprint's reference, with the
#: primary key each one has to be given back once the old column is gone.  `None` means the table
#: keeps its own key and only the scoping column changes.
SPRINT_CHILDREN = (
    ("sprint_repositories", ("sprint_ref", "repository_id")),
    ("sprint_issues", ("sprint_ref", "issue_id")),
    ("sprint_resumes", None),
    ("sprint_projects", ("sprint_ref", "project_id")),
    ("tasks", None),
)

#: The three tables that already carried a *generated* `sprint_ref` for §3.9's claim key.  The
#: column stays, stops being generated, and becomes the scoping column itself.
SPRINT_REF_ALREADY = ("sprint_comments", "sprint_budget_events", "sprint_decisions")


def upgrade() -> None:
    # === §3.2: the Issue's provenance bag (J6). ===
    op.add_column(
        "issues",
        sa.Column(
            "extensions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    # === §3.7 step 1: the third comment table, in §3.7's own shape. ===
    op.create_table(
        "issue_comments",
        sa.Column("comment_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("issue_id", sa.Text(), nullable=False),
        sa.Column("marker", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("actor_role", sa.Text(), nullable=True),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.issue_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("comment_id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index("issue_comments_by_issue", "issue_comments", ["issue_id", "created_at"])

    # === §3.3 and §9: the sprint's reference becomes its identity. ===
    _sprint_identity()

    # === §3.5: the two records that had no representable field. ===
    # `secretary-583` has no `project` metadata; a NOT NULL column would drop the card.
    op.alter_column("tasks", "project_id", existing_type=sa.Text(), nullable=True)
    # Nine `blocked_by` values name cards the board does not hold.  The reference is always kept;
    # the foreign key is set only when it resolves.
    op.drop_constraint("task_dependencies_depends_on_fkey", "task_dependencies", type_="foreignkey")
    op.add_column("task_dependencies", sa.Column("depends_on_task", sa.Text(), nullable=True))
    op.execute("UPDATE task_dependencies SET depends_on_task = depends_on")
    op.create_foreign_key(
        "task_dependencies_depends_on_task_fkey",
        "task_dependencies",
        "tasks",
        ["depends_on_task"],
        ["task_ref"],
    )
    op.create_check_constraint(
        "dependency_resolution_is_the_same_reference",
        "task_dependencies",
        "depends_on_task IS NULL OR depends_on_task = depends_on",
    )

    # === §3.13 step 2: the constraints whose targets are forward or mutual references. ===
    _step_two()


def _sprint_identity() -> None:
    """`sprints.ref` becomes the primary key, and every scoped key follows it there.

    The order is forced: a key cannot be dropped while something references it, and a column
    cannot be dropped while a key uses it.  So the step-2 constraints go first, then the plain
    child foreign keys, then the sprint's own key, and only then does each child swap its column.
    """
    for name, table in (
        ("sprint_current_task_is_in_this_sprint", "sprints"),
        ("sprint_resume_is_of_this_sprint", "sprints"),
        ("budget_card_is_in_this_sprint", "sprint_budget_events"),
        ("decided_issue_is_declared_by_this_sprint", "sprint_decisions"),
        ("decided_card_is_in_this_sprint", "sprint_decisions"),
    ):
        op.drop_constraint(name, table, type_="foreignkey")
    for table, _ in SPRINT_CHILDREN:
        op.drop_constraint(f"{table}_sprint_number_fkey", table, type_="foreignkey")
    for table in SPRINT_REF_ALREADY:
        op.drop_constraint(f"{table}_sprint_number_fkey", table, type_="foreignkey")

    # `ref` stops being computed from the number and starts being the identity.  The stored values
    # survive `DROP EXPRESSION`, so no sprint is renamed by this.
    op.execute("ALTER TABLE sprints ALTER COLUMN ref DROP EXPRESSION")
    op.execute("ALTER TABLE sprints ALTER COLUMN ref SET NOT NULL")
    op.drop_constraint("sprints_ref_key", "sprints", type_="unique")
    op.drop_constraint("sprints_pkey", "sprints", type_="primary")
    op.create_primary_key("sprints_pkey", "sprints", ["ref"])
    op.alter_column("sprints", "sprint_number", existing_type=sa.Integer(), nullable=True)
    op.create_unique_constraint("sprints_sprint_number_key", "sprints", ["sprint_number"])
    op.create_check_constraint("sprint_ref_is_a_sprint_reference", "sprints", "ref ~ '^sprint:'")
    op.create_check_constraint(
        "sprint_number_agrees_with_ref",
        "sprints",
        "(ref ~ '^sprint:[0-9]+$') = (sprint_number IS NOT NULL) AND "
        "(sprint_number IS NULL OR ref = 'sprint:' || sprint_number)",
    )

    for table, primary_key in SPRINT_CHILDREN:
        op.add_column(table, sa.Column("sprint_ref", sa.Text(), nullable=True))
        op.execute(f"UPDATE {table} SET sprint_ref = 'sprint:' || sprint_number")
        # Dropping the column takes the keys built on it with it, which is why the primary key and
        # `tasks`' scoped unique key are recreated below rather than altered.
        op.drop_column(table, "sprint_number")
        if primary_key is not None:
            op.alter_column(table, "sprint_ref", existing_type=sa.Text(), nullable=False)
            op.create_primary_key(f"{table}_pkey", table, list(primary_key))
        op.create_foreign_key(
            f"{table}_sprint_ref_fkey",
            table,
            "sprints",
            ["sprint_ref"],
            ["ref"],
            ondelete=None if table == "tasks" else "CASCADE",
        )
    # The two tables that keep their own primary key and owe a NOT NULL or a scoped unique key.
    op.alter_column("sprint_resumes", "sprint_ref", existing_type=sa.Text(), nullable=False)
    op.create_unique_constraint(
        "sprint_resumes_resume_id_sprint_ref_key", "sprint_resumes", ["resume_id", "sprint_ref"]
    )
    op.create_unique_constraint(
        "tasks_task_ref_sprint_ref_key", "tasks", ["task_ref", "sprint_ref"]
    )

    for table in SPRINT_REF_ALREADY:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN sprint_ref DROP EXPRESSION")
        op.drop_column(table, "sprint_number")
        op.alter_column(table, "sprint_ref", existing_type=sa.Text(), nullable=False)
        op.create_foreign_key(
            f"{table}_sprint_ref_fkey", table, "sprints", ["sprint_ref"], ["ref"], ondelete="CASCADE"
        )
    op.create_index(
        "sprint_decisions_one_per_issue",
        "sprint_decisions",
        ["sprint_ref", "issue_id"],
        unique=True,
        postgresql_where=sa.text("issue_id IS NOT NULL"),
    )
    op.create_index(
        "sprint_decisions_one_per_card",
        "sprint_decisions",
        ["sprint_ref", "task_ref"],
        unique=True,
        postgresql_where=sa.text("task_ref IS NOT NULL"),
    )


def _step_two() -> None:
    """§3.13's second step, for the constraints this revision moved or added."""
    op.create_foreign_key(
        "sprint_current_task_is_in_this_sprint",
        "sprints",
        "tasks",
        ["current_task_ref", "ref"],
        ["task_ref", "sprint_ref"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "sprint_resume_is_of_this_sprint",
        "sprints",
        "sprint_resumes",
        ["resume_id", "ref"],
        ["resume_id", "sprint_ref"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "budget_card_is_in_this_sprint",
        "sprint_budget_events",
        "tasks",
        ["task_ref", "sprint_ref"],
        ["task_ref", "sprint_ref"],
    )
    op.create_foreign_key(
        "decided_issue_is_declared_by_this_sprint",
        "sprint_decisions",
        "sprint_issues",
        ["sprint_ref", "issue_id"],
        ["sprint_ref", "issue_id"],
    )
    op.create_foreign_key(
        "decided_card_is_in_this_sprint",
        "sprint_decisions",
        "tasks",
        ["task_ref", "sprint_ref"],
        ["task_ref", "sprint_ref"],
    )
    # §3.9's claim key for the new comment table, on the generated ref it joins `requests` by.
    op.add_column("issue_comments", sa.Column("issue_ref", sa.Text(), ISSUE_REF, nullable=True))
    op.create_foreign_key(
        "issue_comment_claims_its_request",
        "issue_comments",
        "requests",
        ["request_id", "issue_ref"],
        ["request_id", "ref"],
    )


def downgrade() -> None:
    """There is none, for `0001_initial`'s reason (§7.4).

    A revision that fails rolls back whole, so the recovery is to fix the revision and run it
    again.  Unwinding this one would put back a primary key that cannot hold two of the sprints
    the board carries.
    """
    raise NotImplementedError(
        "the board store has no down migration: a failed revision rolls back whole (§7.4), and "
        "unwinding this one would restore a sprint key that cannot hold every sprint on the board"
    )
