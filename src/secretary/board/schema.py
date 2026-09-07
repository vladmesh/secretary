"""The board store's schema, as SQLAlchemy models: the one source of truth for §3.

``docs/BOARD_STORE.md`` §3 remains the *description* of the target schema — it is what a reader
argues with — and this module is that description made executable.  The owner's decision of
2026-09-07 replaced the in-product SQL runner with SQLAlchemy and Alembic, so the schema is no
longer a string of DDL shipped in the tree: it is these declarations, and the Alembic revision
that builds an empty database is checked against them (``tests/test_board_store_schema.py``
compares the migrated database with this metadata and fails on any drift).

Everything §3 constrains is declared here, including the parts an ORM does not express by itself:

* every closed vocabulary of §3.12 is a `CheckConstraint`, never a reference table;
* the four partial unique indexes of §3.6 and §3.8 are `Index(..., postgresql_where=...)`;
* the three `ref`-shaped generated columns are `Computed(..., persisted=True)`, PostgreSQL
  generated columns — `products.ref`, `issues.ref` and `issue_comments.issue_ref`;
* §3.3's two scoped sprint cursors are `DEFERRABLE INITIALLY DEFERRED` composite foreign keys,
  and every constraint §3.13 defers to step 2 carries ``use_alter=True`` so it is emitted as an
  ``ALTER TABLE`` after the tables exist, exactly as §3.13 orders it;
* the six `jsonb` columns are the six §3.10 names and no others.

Revision `0002_board_gaps` moved four things here, each named by the import run of
`secretary-1583` on the live board (2026-09-07) that found it: `issue_comments` (479 comments on
Issue rows had no table), `issues.extensions` (nine metadata keys on 158 Issue rows had no home),
the sprint's identity (`sprints.ref` is the primary key and `sprint_number` a nullable unique
number, because two live sprints are `sprint:canary-terra-20260813` and
`sprint:canary-terra-final-20260813` and an `integer` primary key cannot hold them), and the two
records that had no representable field — a card with no `project` metadata and nine `blocked_by`
values naming cards that are not on the board.

Revision `0003_task_type_optional` is the last of that same list: the import run of
`secretary-1585` (2026-09-07) named one card the store still could not hold, `secretary-583`
again, this time for carrying no `task_type` metadata against a `NOT NULL` column.  The column is
nullable now and its `CHECK` admits NULL or a value of the closed vocabulary — the board's silence,
stored as silence.

The version table is Alembic's ``alembic_version`` and is not declared here: it is the migration
tool's own bookkeeping, it is created by the tool, and inventing a second one beside it is what
§7.4 no longer does.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase

#: `timestamptz`, the only time type in this schema (§3).
TIMESTAMPTZ = TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    pass


metadata = Base.metadata

#: §9: the allocator for `sprint:N`.  A standalone sequence, not a column default: §9 hands the
#: number out before the row exists.
SPRINT_NUMBER_SEQ = sa.Sequence("sprint_number_seq", metadata=metadata)


# --- §3.1 Products, projects, repositories ------------------------------------------------


class Product(Base):
    __tablename__ = "products"

    product_id = sa.Column(sa.Text, primary_key=True)  # "secretary"
    ref = sa.Column(sa.Text, sa.Computed("'product:' || product_id", persisted=True))
    title = sa.Column(sa.Text, nullable=False)
    description = sa.Column(sa.Text, nullable=False, server_default=sa.text("''"))
    state = sa.Column(sa.Text, nullable=False, server_default=sa.text("'active'"))
    created_at = sa.Column(TIMESTAMPTZ, nullable=False)
    updated_at = sa.Column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("ref"),
        sa.CheckConstraint("title <> ''"),
        sa.CheckConstraint("state IN ('active','archived')"),
    )


class Project(Base):
    __tablename__ = "projects"

    project_id = sa.Column(sa.Text, primary_key=True)  # registry id, e.g. "secretary"
    enabled = sa.Column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    plane = sa.Column(sa.Text, nullable=False, server_default=sa.text("'project'"))
    adapter = sa.Column(sa.Text)
    orca_binding = sa.Column(sa.Text)
    # false: referenced by history, absent from the registry
    registry_present = sa.Column(sa.Boolean, nullable=False, server_default=sa.text("true"))


class Repository(Base):
    __tablename__ = "repositories"

    repository_id = sa.Column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    project_id = sa.Column(sa.Text, sa.ForeignKey("projects.project_id"))
    path = sa.Column(sa.Text, nullable=False)  # absolute working-tree path
    remote = sa.Column(sa.Text)
    default_branch = sa.Column(sa.Text, nullable=False, server_default=sa.text("'main'"))
    role = sa.Column(sa.Text, nullable=False, server_default=sa.text("'primary'"))

    __table_args__ = (
        sa.CheckConstraint("role IN ('primary','curator_root')"),
        sa.UniqueConstraint("path"),
        sa.Index(
            "repositories_one_primary",
            "project_id",
            unique=True,
            postgresql_where=sa.text("role = 'primary'"),
        ),
    )


class ProductProject(Base):
    """Product.projects, today a JSON array."""

    __tablename__ = "product_projects"

    product_id = sa.Column(
        sa.Text, sa.ForeignKey("products.product_id", ondelete="CASCADE"), primary_key=True
    )
    project_id = sa.Column(sa.Text, sa.ForeignKey("projects.project_id"), primary_key=True)


# --- §3.2 Issues --------------------------------------------------------------------------


class Issue(Base):
    __tablename__ = "issues"

    issue_id = sa.Column(sa.Text, primary_key=True)  # the 20-hex suffix of issue:<id>
    ref = sa.Column(sa.Text, sa.Computed("'issue:' || issue_id", persisted=True))
    product_id = sa.Column(sa.Text, sa.ForeignKey("products.product_id"), nullable=False)
    title = sa.Column(sa.Text, nullable=False)
    description = sa.Column(sa.Text, nullable=False, server_default=sa.text("''"))
    issue_kind = sa.Column(sa.Text, nullable=False)
    priority = sa.Column(sa.Text, nullable=False)
    state = sa.Column(sa.Text, nullable=False, server_default=sa.text("'open'"))
    close_reason = sa.Column(sa.Text)
    # 0002: nine leftover metadata keys ride on 158 Issue rows, and 72 Issues sit in a lane that
    # is not their product's.  Without this column the import drops them; with it they are
    # provenance a query can find, exactly as `tasks.extensions` is (§8.2).
    extensions = sa.Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))  # (J6)
    created_at = sa.Column(TIMESTAMPTZ, nullable=False)
    updated_at = sa.Column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("ref"),
        sa.CheckConstraint("title <> ''"),
        sa.CheckConstraint("issue_kind IN ('bug','feature','question','improvement')"),
        sa.CheckConstraint("priority IN ('P0','P1','P2','P3')"),
        sa.CheckConstraint("state IN ('open','closed')"),
        sa.CheckConstraint("close_reason IN ('resolved','invalid','duplicate','wont_do')"),
        sa.CheckConstraint(
            "(state = 'closed') = (close_reason IS NOT NULL)",
            name="issue_close_reason_matches_state",
        ),
    )


# --- §3.3 Sprints -------------------------------------------------------------------------


class Sprint(Base):
    __tablename__ = "sprints"

    # §9's stable identifier *is* the identity: a reference the board carries is representable
    # here whatever it spells.  `sprint_number` stays for §9's allocator and for the numbering
    # rule, as a nullable unique column rather than as the key.
    ref = sa.Column(sa.Text, primary_key=True)  # "sprint:1037", "sprint:canary-terra-20260813"
    sprint_number = sa.Column(sa.Integer, autoincrement=False)  # N in sprint:N, NULL when unnumbered
    goal = sa.Column(sa.Text, nullable=False)
    definition_of_done = sa.Column(sa.Text, nullable=False)
    product_id = sa.Column(sa.Text, sa.ForeignKey("products.product_id"))
    status = sa.Column(sa.Text, nullable=False, server_default=sa.text("'open'"))
    observer = sa.Column(JSONB)  # (J1)
    worker_pin = sa.Column(sa.Text)
    reviewer_pin = sa.Column(sa.Text)
    # Both cursors are scoped to this sprint by composite foreign key, not by a bare
    # existence check.  See "Scoped relations" in §3.3.
    current_task_ref = sa.Column(sa.Text)
    resume_id = sa.Column(sa.BigInteger)
    close_reason = sa.Column(sa.Text)
    closeout_document = sa.Column(sa.Text)  # state/knowledge path, not the prose
    source_audit = sa.Column(JSONB)  # (J2)
    created_at = sa.Column(TIMESTAMPTZ, nullable=False)
    updated_at = sa.Column(TIMESTAMPTZ, nullable=False)
    closed_at = sa.Column(TIMESTAMPTZ)

    __table_args__ = (
        sa.UniqueConstraint("sprint_number"),
        sa.CheckConstraint("ref ~ '^sprint:'", name="sprint_ref_is_a_sprint_reference"),
        # A numbered reference keeps its number, and only a numbered reference has one: this is
        # what stops `sprint_number_seq` from handing out a number some `sprint:N` already spells.
        sa.CheckConstraint(
            "(ref ~ '^sprint:[0-9]+$') = (sprint_number IS NOT NULL) AND "
            "(sprint_number IS NULL OR ref = 'sprint:' || sprint_number)",
            name="sprint_number_agrees_with_ref",
        ),
        sa.CheckConstraint("status IN ('open','closed','stopped')"),
        sa.CheckConstraint("(status = 'open') = (closed_at IS NULL)", name="sprint_closed_has_time"),
        # §3.13 step 2: `tasks` and `sprint_resumes` do not exist yet, and both relations are
        # mutual, so these are emitted as ALTER TABLE after every table is created.
        sa.ForeignKeyConstraint(
            ["current_task_ref", "ref"],
            ["tasks.task_ref", "tasks.sprint_ref"],
            name="sprint_current_task_is_in_this_sprint",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["resume_id", "ref"],
            ["sprint_resumes.resume_id", "sprint_resumes.sprint_ref"],
            name="sprint_resume_is_of_this_sprint",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )


class SprintRepository(Base):
    __tablename__ = "sprint_repositories"

    sprint_ref = sa.Column(
        sa.Text, sa.ForeignKey("sprints.ref", ondelete="CASCADE"), primary_key=True
    )
    repository_id = sa.Column(
        sa.BigInteger, sa.ForeignKey("repositories.repository_id"), primary_key=True
    )


class SprintIssue(Base):
    __tablename__ = "sprint_issues"

    sprint_ref = sa.Column(
        sa.Text, sa.ForeignKey("sprints.ref", ondelete="CASCADE"), primary_key=True
    )
    issue_id = sa.Column(sa.Text, sa.ForeignKey("issues.issue_id"), primary_key=True)


class SprintResume(Base):
    """Append-only; `sprints.resume_id` names the live one."""

    __tablename__ = "sprint_resumes"

    resume_id = sa.Column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    sprint_ref = sa.Column(
        sa.Text, sa.ForeignKey("sprints.ref", ondelete="CASCADE"), nullable=False
    )
    selected_step = sa.Column(sa.Text, nullable=False)
    selected_why = sa.Column(sa.Text, nullable=False)
    rejected_alternatives = sa.Column(sa.Text, nullable=False)
    current_task = sa.Column(sa.Text, nullable=False)
    dod_state = sa.Column(sa.Text, nullable=False)
    next_safe_step = sa.Column(sa.Text, nullable=False)
    recorded_at = sa.Column(TIMESTAMPTZ, nullable=False)

    # The target a scoped foreign key needs; redundant with the primary key by design.
    __table_args__ = (sa.UniqueConstraint("resume_id", "sprint_ref"),)


# --- §3.4 Budget --------------------------------------------------------------------------


class SprintBudgetEvent(Base):
    __tablename__ = "sprint_budget_events"

    budget_event_id = sa.Column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    # Since the sprint's identity is its reference, this is the reference itself: the generated
    # `sprint_ref` column §3.9's claim key used to need is now the scoping column.
    sprint_ref = sa.Column(
        sa.Text, sa.ForeignKey("sprints.ref", ondelete="CASCADE"), nullable=False
    )
    event_type = sa.Column(sa.Text, nullable=False)
    charged = sa.Column(sa.Boolean, nullable=False)
    task_ref = sa.Column(sa.Text)
    reason = sa.Column(sa.Text, nullable=False)
    request_id = sa.Column(sa.Text, nullable=False)  # references `requests`; see §3.9
    occurred_at = sa.Column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("request_id"),
        sa.CheckConstraint(
            "event_type IN ('red_review','blocked','red_ci','preempt','recreated_task','hotfix',"
            "'infrastructure_blocked')"
        ),
        sa.CheckConstraint(
            "charged = (event_type <> 'infrastructure_blocked')", name="budget_charge_matches_type"
        ),
        sa.ForeignKeyConstraint(
            ["task_ref", "sprint_ref"],
            ["tasks.task_ref", "tasks.sprint_ref"],
            name="budget_card_is_in_this_sprint",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "sprint_ref"],
            ["requests.request_id", "requests.ref"],
            name="budget_event_claims_its_request",
            use_alter=True,
        ),
    )


# --- §3.5 Cards (tasks) -------------------------------------------------------------------


class Task(Base):
    __tablename__ = "tasks"

    task_ref = sa.Column(sa.Text, primary_key=True)  # "secretary-1580"
    # Nullable since 0002: `secretary-583` carries no `project` metadata, and a NOT NULL column
    # would have made that card the one record the board holds and the store cannot (§8.6).
    project_id = sa.Column(sa.Text, sa.ForeignKey("projects.project_id"))
    task_number = sa.Column(sa.Integer, nullable=False)
    title = sa.Column(sa.Text, nullable=False)
    description = sa.Column(sa.Text, nullable=False, server_default=sa.text("''"))
    # Nullable since 0003: `secretary-583` carries no `task_type` metadata either, and the
    # board's silence is stored as NULL rather than as an invented type (§8.6).
    task_type = sa.Column(sa.Text)
    state = sa.Column(sa.Text, nullable=False)
    archived = sa.Column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    position = sa.Column(sa.Integer, nullable=False, server_default=sa.text("0"))
    sprint_ref = sa.Column(sa.Text, sa.ForeignKey("sprints.ref"))
    claim_worker = sa.Column(sa.Text)
    claimed_at = sa.Column(TIMESTAMPTZ)
    # workspace
    slug = sa.Column(sa.Text)
    base_branch = sa.Column(sa.Text)
    seed_ref = sa.Column(sa.Text)
    # routing
    complexity = sa.Column(sa.Text, nullable=False, server_default=sa.text("'standard'"))
    family_preference = sa.Column(sa.Text, nullable=False, server_default=sa.text("'auto'"))
    head_override = sa.Column(sa.Text)
    review_head_override = sa.Column(sa.Text)
    resolved_worker_head = sa.Column(sa.Text)
    resolved_worker_family = sa.Column(sa.Text)
    resolved_review_head = sa.Column(sa.Text)
    resolved_review_family = sa.Column(sa.Text)
    routing_reason = sa.Column(sa.Text)
    quota_snapshot_at = sa.Column(TIMESTAMPTZ)
    codex_launch_mode = sa.Column(sa.Text)
    retry_same = sa.Column(sa.Integer, nullable=False, server_default=sa.text("0"))
    retry_switch = sa.Column(sa.Integer, nullable=False, server_default=sa.text("0"))
    extensions = sa.Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))  # (J3)
    created_at = sa.Column(TIMESTAMPTZ, nullable=False)
    updated_at = sa.Column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        sa.CheckConstraint("title <> ''"),
        sa.CheckConstraint(
            "task_type IS NULL OR task_type IN ('code','research')",
            name="task_type_is_a_known_type_or_nothing",
        ),
        sa.CheckConstraint(
            "state IN ('issues','ready','in_progress','validate','assessment','blocked','done')"
        ),
        sa.CheckConstraint("complexity IN ('cheap','standard','hard','frontier')"),
        sa.CheckConstraint("family_preference IN ('auto','claude','codex')"),
        sa.CheckConstraint("codex_launch_mode IN ('tui')"),
        sa.CheckConstraint("retry_same >= 0"),
        sa.CheckConstraint("retry_switch >= 0"),
        sa.UniqueConstraint("project_id", "task_number"),
        # The target the sprint's scoped cursor and decision keys need (§3.3, §3.8).
        # Redundant with the primary key by design.
        sa.UniqueConstraint("task_ref", "sprint_ref"),
    )


class TaskRetryHead(Base):
    """`retry_heads`, today a delimited string."""

    __tablename__ = "task_retry_heads"

    task_ref = sa.Column(
        sa.Text, sa.ForeignKey("tasks.task_ref", ondelete="CASCADE"), primary_key=True
    )
    ordinal = sa.Column(sa.Integer, primary_key=True, autoincrement=False)
    head = sa.Column(sa.Text, nullable=False)


class TaskIssue(Base):
    __tablename__ = "task_issues"

    task_ref = sa.Column(
        sa.Text, sa.ForeignKey("tasks.task_ref", ondelete="CASCADE"), primary_key=True
    )
    issue_id = sa.Column(sa.Text, sa.ForeignKey("issues.issue_id"), primary_key=True)


class TaskDependency(Base):
    """`blocked_by`.

    Two columns since 0002, because nine `blocked_by` values on this board name cards that are
    not on it (`triggered-agents-*`, `memory-mcp-*`).  `depends_on` is the reference as the card
    writes it and is always kept; `depends_on_task` is the same reference *as a foreign key* and
    is set exactly when the board holds that card, so a dependency that resolves is still checked
    relationally and one that does not is still a row (§8.6).
    """

    __tablename__ = "task_dependencies"

    task_ref = sa.Column(
        sa.Text, sa.ForeignKey("tasks.task_ref", ondelete="CASCADE"), primary_key=True
    )
    depends_on = sa.Column(sa.Text, primary_key=True)
    depends_on_task = sa.Column(sa.Text, sa.ForeignKey("tasks.task_ref"))

    __table_args__ = (
        sa.CheckConstraint("task_ref <> depends_on", name="no_self_dependency"),
        sa.CheckConstraint(
            "depends_on_task IS NULL OR depends_on_task = depends_on",
            name="dependency_resolution_is_the_same_reference",
        ),
    )


class TaskSupersession(Base):
    """`supersedes`."""

    __tablename__ = "task_supersessions"

    task_ref = sa.Column(
        sa.Text, sa.ForeignKey("tasks.task_ref", ondelete="CASCADE"), primary_key=True
    )
    supersedes = sa.Column(sa.Text, sa.ForeignKey("tasks.task_ref"), nullable=False)
    recorded_at = sa.Column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (sa.CheckConstraint("task_ref <> supersedes", name="no_self_supersession"),)


# --- §3.6 Reservations --------------------------------------------------------------------


class SprintProject(Base):
    __tablename__ = "sprint_projects"

    sprint_ref = sa.Column(
        sa.Text, sa.ForeignKey("sprints.ref", ondelete="CASCADE"), primary_key=True
    )
    project_id = sa.Column(sa.Text, sa.ForeignKey("projects.project_id"), primary_key=True)
    reserved = sa.Column(sa.Boolean, nullable=False, server_default=sa.text("true"))
    reserved_at = sa.Column(TIMESTAMPTZ, nullable=False)
    released_at = sa.Column(TIMESTAMPTZ)

    __table_args__ = (
        sa.CheckConstraint("reserved = (released_at IS NULL)", name="reserved_matches_release"),
        sa.Index(
            "sprint_projects_one_live_reservation",
            "project_id",
            unique=True,
            postgresql_where=sa.text("reserved"),
        ),
    )


# --- §3.7 Comments ------------------------------------------------------------------------


class SprintComment(Base):
    __tablename__ = "sprint_comments"

    comment_id = sa.Column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    sprint_ref = sa.Column(
        sa.Text, sa.ForeignKey("sprints.ref", ondelete="CASCADE"), nullable=False
    )
    marker = sa.Column(sa.Text)  # "po", "sprint:resume", NULL for unmarked
    body = sa.Column(sa.Text, nullable=False)
    actor_role = sa.Column(sa.Text)
    actor_id = sa.Column(sa.Text)
    # Unique *per claimed request*; the namespace itself is `requests` (§3.9), which is
    # created later, so the foreign key is a §3.13 step 2 constraint.
    request_id = sa.Column(sa.Text)
    created_at = sa.Column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("request_id"),
        sa.ForeignKeyConstraint(
            ["request_id", "sprint_ref"],
            ["requests.request_id", "requests.ref"],
            name="sprint_comment_claims_its_request",
            use_alter=True,
        ),
    )


class TaskComment(Base):
    __tablename__ = "task_comments"

    comment_id = sa.Column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    task_ref = sa.Column(
        sa.Text, sa.ForeignKey("tasks.task_ref", ondelete="CASCADE"), nullable=False
    )
    marker = sa.Column(sa.Text)  # role, or report:/review:/decision:*
    body = sa.Column(sa.Text, nullable=False)
    actor_role = sa.Column(sa.Text)
    actor_id = sa.Column(sa.Text)
    request_id = sa.Column(sa.Text)  # FK added in §3.13 step 2
    created_at = sa.Column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("request_id"),
        sa.Index("task_comments_by_task", "task_ref", "created_at"),
        sa.ForeignKeyConstraint(
            ["request_id", "task_ref"],
            ["requests.request_id", "requests.ref"],
            name="task_comment_claims_its_request",
            use_alter=True,
        ),
    )


class IssueComment(Base):
    """The third comment table, added by 0002.

    `secretary-1583` read the live board and found 479 comments on Issue rows with nowhere to go:
    §3.7 declared two comment tables and both are foreign-keyed to their own entity, so a comment
    on an Issue was the largest single record loss the import found.  This is §3.7's shape again,
    unchanged — the same columns, the same claim key, the same place in §3.13 — with `issues` as
    the entity.  The same read counted **0** comments on Product rows, so there is no
    `product_comments` table and the counted zero is what §3.7 records instead.
    """

    __tablename__ = "issue_comments"

    comment_id = sa.Column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    issue_id = sa.Column(
        sa.Text, sa.ForeignKey("issues.issue_id", ondelete="CASCADE"), nullable=False
    )
    marker = sa.Column(sa.Text)  # role, or issue:* — the same vocabulary §8.1 lists
    body = sa.Column(sa.Text, nullable=False)
    actor_role = sa.Column(sa.Text)
    actor_id = sa.Column(sa.Text)
    request_id = sa.Column(sa.Text)  # FK added in §3.13 step 2
    created_at = sa.Column(TIMESTAMPTZ, nullable=False)
    # §3.13 step 2: the generated ref the composite claim key joins on, exactly as the sprint
    # tables did before the sprint's identity became its reference.
    issue_ref = sa.Column(sa.Text, sa.Computed("'issue:' || issue_id", persisted=True))

    __table_args__ = (
        sa.UniqueConstraint("request_id"),
        sa.Index("issue_comments_by_issue", "issue_id", "created_at"),
        sa.ForeignKeyConstraint(
            ["request_id", "issue_ref"],
            ["requests.request_id", "requests.ref"],
            name="issue_comment_claims_its_request",
            use_alter=True,
        ),
    )


# --- §3.8 Sprint decisions ----------------------------------------------------------------


class SprintDecision(Base):
    __tablename__ = "sprint_decisions"

    decision_id = sa.Column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    sprint_ref = sa.Column(
        sa.Text, sa.ForeignKey("sprints.ref", ondelete="CASCADE"), nullable=False
    )
    subject_kind = sa.Column(sa.Text, nullable=False)
    # Both subjects are scoped to this sprint below; neither is a bare existence check.
    issue_id = sa.Column(sa.Text)
    task_ref = sa.Column(sa.Text)
    verdict = sa.Column(sa.Text, nullable=False)
    actual = sa.Column(sa.Text)
    reason = sa.Column(sa.Text, nullable=False)
    # References `requests`, deliberately NOT unique: one close claims one id and
    # writes one decision per declared issue and per remaining card (§3.9).
    request_id = sa.Column(sa.Text, nullable=False)
    decided_at = sa.Column(TIMESTAMPTZ, nullable=False)

    __table_args__ = (
        sa.CheckConstraint("subject_kind IN ('issue','card')"),
        sa.CheckConstraint(
            "(subject_kind = 'issue' AND issue_id IS NOT NULL AND task_ref IS NULL) OR "
            "(subject_kind = 'card'  AND task_ref IS NOT NULL AND issue_id IS NULL)",
            name="decision_subject_is_exactly_one",
        ),
        sa.CheckConstraint(
            "(subject_kind = 'issue' AND verdict IN "
            "('resolved','invalid','duplicate','wont_do','open','already_closed')) OR "
            "(subject_kind = 'card'  AND verdict IN ('done','drop','already_moved'))",
            name="decision_verdict_in_vocabulary",
        ),
        sa.CheckConstraint(
            "(verdict IN ('already_closed','already_moved')) = (actual IS NOT NULL)",
            name="decision_actual_only_on_confirmation",
        ),
        sa.CheckConstraint("reason <> ''"),
        sa.Index(
            "sprint_decisions_one_per_issue",
            "sprint_ref",
            "issue_id",
            unique=True,
            postgresql_where=sa.text("issue_id IS NOT NULL"),
        ),
        sa.Index(
            "sprint_decisions_one_per_card",
            "sprint_ref",
            "task_ref",
            unique=True,
            postgresql_where=sa.text("task_ref IS NOT NULL"),
        ),
        sa.ForeignKeyConstraint(
            ["sprint_ref", "issue_id"],
            ["sprint_issues.sprint_ref", "sprint_issues.issue_id"],
            name="decided_issue_is_declared_by_this_sprint",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["task_ref", "sprint_ref"],
            ["tasks.task_ref", "tasks.sprint_ref"],
            name="decided_card_is_in_this_sprint",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["request_id", "sprint_ref"],
            ["requests.request_id", "requests.ref"],
            name="decision_belongs_to_its_close_request",
            use_alter=True,
        ),
    )


# --- §3.9 Request ownership, events and idempotency ---------------------------------------


class Request(Base):
    __tablename__ = "requests"

    request_id = sa.Column(sa.Text, primary_key=True)  # the whole installation's one namespace
    operation = sa.Column(sa.Text, nullable=False)  # "card.comment", "sprint.close", …
    intent = sa.Column(JSONB, nullable=False)  # (J5) frozen at claim, never updated
    status = sa.Column(sa.Text, nullable=False)
    # A protocol occurrence may never be replaced by a generic stage; see §3.9's rule.
    protocol = sa.Column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    entity_kind = sa.Column(sa.Text)
    ref = sa.Column(sa.Text)
    created_at = sa.Column(TIMESTAMPTZ, nullable=False)
    settled_at = sa.Column(TIMESTAMPTZ)

    __table_args__ = (
        sa.CheckConstraint("status IN ('staged','committed','discarded')"),
        sa.CheckConstraint("entity_kind IN ('product','issue','sprint','card')"),
        sa.CheckConstraint(
            "(status = 'staged') = (settled_at IS NULL)", name="request_settled_matches_status"
        ),
        # The target of every child's composite claim key.  Redundant with the primary key by
        # design, exactly as the scoped sprint keys of §3.3 are.
        sa.UniqueConstraint("request_id", "ref", name="requests_ref_identity"),
    )


class BoardEvent(Base):
    __tablename__ = "board_events"

    # globally unique, as TaskAudit.event_id_owner requires
    event_id = sa.Column(sa.Text, primary_key=True)
    request_id = sa.Column(sa.Text, sa.ForeignKey("requests.request_id"), nullable=False)
    kind = sa.Column(sa.Text, nullable=False)
    entity_kind = sa.Column(sa.Text, nullable=False)
    ref = sa.Column(sa.Text, nullable=False)
    actor_role = sa.Column(sa.Text, nullable=False)
    actor_id = sa.Column(sa.Text, nullable=False)
    head_run_ref = sa.Column(sa.Text)
    reason = sa.Column(sa.Text, nullable=False)
    source_state = sa.Column(sa.Text)
    target_state = sa.Column(sa.Text)
    related_refs = sa.Column(ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'"))
    data = sa.Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))  # (J4)
    occurred_at = sa.Column(TIMESTAMPTZ, nullable=False)
    committed = sa.Column(sa.Boolean, nullable=False, server_default=sa.text("false"))
    committed_at = sa.Column(TIMESTAMPTZ)

    __table_args__ = (
        sa.UniqueConstraint("request_id"),
        sa.CheckConstraint(  # EventKind, §3.12
            "kind IN ("
            "'entity.created','entity.updated','product.archived','issue.closed',"
            "'sprint.closed','sprint.stopped','sprint.reopened',"
            "'card.readied','card.started','card.submitted','card.assessed','card.reworked',"
            "'card.released','card.blocked','card.unblocked','card.returned','card.moved',"
            "'card.reported','card.verdict','card.decided','card.decision_refused',"
            "'attempt.usage','attempt.outcome')"
        ),
        sa.CheckConstraint("entity_kind IN ('product','issue','sprint','card')"),
        sa.Index("board_events_by_ref", "ref", "occurred_at"),
        sa.ForeignKeyConstraint(
            ["request_id", "ref"],
            ["requests.request_id", "requests.ref"],
            name="board_event_claims_its_request",
            use_alter=True,
        ),
    )


#: The three §5.5 role names.  They are literals of the design, not of one installation:
#: `board-store.env` carries the *passwords*, which is what the revision takes as parameters.
OWNER_ROLE = "secretary_owner"
APP_ROLE = "secretary_app"
READ_ROLE = "secretary_read"

#: The generated credentials §5.5's fence needs, named the way the revision asks for them.
PASSWORD_PARAMETERS = ("app_password", "read_password")

#: The columns §3.10 declares `jsonb`, and the only ones in the schema.
JSONB_COLUMNS = (
    ("sprints", "observer"),
    ("sprints", "source_audit"),
    ("tasks", "extensions"),
    ("issues", "extensions"),
    ("board_events", "data"),
    ("requests", "intent"),
)

#: What §3.13 defers to step 2: every constraint emitted as `ALTER TABLE` after the tables.
DEFERRED_CONSTRAINTS = (
    "sprint_current_task_is_in_this_sprint",
    "sprint_resume_is_of_this_sprint",
    "budget_card_is_in_this_sprint",
    "decided_issue_is_declared_by_this_sprint",
    "decided_card_is_in_this_sprint",
    "board_event_claims_its_request",
    "task_comment_claims_its_request",
    "issue_comment_claims_its_request",
    "sprint_comment_claims_its_request",
    "budget_event_claims_its_request",
    "decision_belongs_to_its_close_request",
)

__all__ = [
    "APP_ROLE",
    "DEFERRED_CONSTRAINTS",
    "JSONB_COLUMNS",
    "OWNER_ROLE",
    "PASSWORD_PARAMETERS",
    "READ_ROLE",
    "SPRINT_NUMBER_SEQ",
    "TIMESTAMPTZ",
    "Base",
    "BoardEvent",
    "Issue",
    "IssueComment",
    "Product",
    "ProductProject",
    "Project",
    "Repository",
    "Request",
    "Sprint",
    "SprintBudgetEvent",
    "SprintComment",
    "SprintDecision",
    "SprintIssue",
    "SprintProject",
    "SprintRepository",
    "SprintResume",
    "Task",
    "TaskComment",
    "TaskDependency",
    "TaskIssue",
    "TaskRetryHead",
    "TaskSupersession",
    "metadata",
]
