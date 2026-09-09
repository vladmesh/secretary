"""The initial board-store schema: §3's tables in §3.13's order, then §5.5's roles.

Alembic's first revision, and the only one this build ships.  It is the executable half of
``docs/BOARD_STORE.md``: `secretary.board.schema` declares what the schema *is*, this revision is
what builds it on an empty database, and `tests/test_board_store_schema.py` runs it against a real
`postgres:16` and then asks Alembic itself whether the result still matches the models — so the
two cannot drift without a red test.

The structure is §3.13's, and deliberately visible as such:

* **step 1** creates the tables in §3 reading order, plus §9's `sprint_number_seq`;
* **step 2** adds the constraints whose targets are forward or mutual references — §3.3's two
  scoped sprint cursors, §3.4's `budget_card_is_in_this_sprint`, §3.8's two scoped decision
  subjects, §3.9's three generated `sprint_ref` columns and its five `request_id` foreign keys;
* **§5.5** comes last, so that `GRANT ... ON ALL TABLES` reaches every table created above and
  `ALTER DEFAULT PRIVILEGES` reaches the tables every later revision adds.

The two passwords §5.5's `CREATE ROLE` statements need are **parameters of the run**, taken from
`board-store.env` and handed over in `config.attributes["passwords"]` by
`secretary.board.migrate`.  They are never bytes of this file.

There is no `schema_migrations` table any more: the version is Alembic's `alembic_version`, which
is the owner's decision of 2026-09-07 and the reason this file exists at all.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from secretary.board.schema import APP_ROLE, OWNER_ROLE, PASSWORD_PARAMETERS, READ_ROLE

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

SPRINT_REF = sa.Computed("'sprint:' || sprint_number", persisted=True)


def upgrade() -> None:
    # === Step 1: create the tables, in §3 reading order (§3.13). ===

    # --- §3.1 Products, projects, repositories
    op.create_table('products',
    sa.Column('product_id', sa.Text(), nullable=False),
    sa.Column('ref', sa.Text(), sa.Computed("'product:' || product_id", persisted=True), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('state', sa.Text(), server_default=sa.text("'active'"), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint("state IN ('active','archived')"),
    sa.CheckConstraint("title <> ''"),
    sa.PrimaryKeyConstraint('product_id'),
    sa.UniqueConstraint('ref')
    )
    op.create_table('projects',
    sa.Column('project_id', sa.Text(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('plane', sa.Text(), server_default=sa.text("'project'"), nullable=False),
    sa.Column('adapter', sa.Text(), nullable=True),
    sa.Column('orca_binding', sa.Text(), nullable=True),
    sa.Column('registry_present', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.PrimaryKeyConstraint('project_id')
    )
    op.create_table('repositories',
    sa.Column('repository_id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('project_id', sa.Text(), nullable=True),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('remote', sa.Text(), nullable=True),
    sa.Column('default_branch', sa.Text(), server_default=sa.text("'main'"), nullable=False),
    sa.Column('role', sa.Text(), server_default=sa.text("'primary'"), nullable=False),
    sa.CheckConstraint("role IN ('primary','curator_root')"),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.PrimaryKeyConstraint('repository_id'),
    sa.UniqueConstraint('path')
    )
    op.create_index('repositories_one_primary', 'repositories', ['project_id'], unique=True, postgresql_where=sa.text("role = 'primary'"))
    op.create_table('product_projects',
    sa.Column('product_id', sa.Text(), nullable=False),
    sa.Column('project_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.product_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.PrimaryKeyConstraint('product_id', 'project_id')
    )

    # --- §3.2 Issues
    op.create_table('issues',
    sa.Column('issue_id', sa.Text(), nullable=False),
    sa.Column('ref', sa.Text(), sa.Computed("'issue:' || issue_id", persisted=True), nullable=True),
    sa.Column('product_id', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('issue_kind', sa.Text(), nullable=False),
    sa.Column('priority', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), server_default=sa.text("'open'"), nullable=False),
    sa.Column('close_reason', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint("(state = 'closed') = (close_reason IS NOT NULL)", name='issue_close_reason_matches_state'),
    sa.CheckConstraint("close_reason IN ('resolved','invalid','duplicate','wont_do')"),
    sa.CheckConstraint("issue_kind IN ('bug','feature','question','improvement')"),
    sa.CheckConstraint("priority IN ('P0','P1','P2','P3')"),
    sa.CheckConstraint("state IN ('open','closed')"),
    sa.CheckConstraint("title <> ''"),
    sa.ForeignKeyConstraint(['product_id'], ['products.product_id'], ),
    sa.PrimaryKeyConstraint('issue_id'),
    sa.UniqueConstraint('ref')
    )

    # --- §3.3 Sprints
    op.create_table('sprints',
    sa.Column('sprint_number', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('ref', sa.Text(), sa.Computed("'sprint:' || sprint_number", persisted=True), nullable=True),
    sa.Column('goal', sa.Text(), nullable=False),
    sa.Column('definition_of_done', sa.Text(), nullable=False),
    sa.Column('product_id', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default=sa.text("'open'"), nullable=False),
    sa.Column('observer', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('worker_pin', sa.Text(), nullable=True),
    sa.Column('reviewer_pin', sa.Text(), nullable=True),
    sa.Column('current_task_ref', sa.Text(), nullable=True),
    sa.Column('resume_id', sa.BigInteger(), nullable=True),
    sa.Column('close_reason', sa.Text(), nullable=True),
    sa.Column('closeout_document', sa.Text(), nullable=True),
    sa.Column('source_audit', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('closed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.CheckConstraint("(status = 'open') = (closed_at IS NULL)", name='sprint_closed_has_time'),
    sa.CheckConstraint("status IN ('open','closed','stopped')"),
    sa.ForeignKeyConstraint(['product_id'], ['products.product_id'], ),
    sa.PrimaryKeyConstraint('sprint_number'),
    sa.UniqueConstraint('ref')
    )
    op.execute(sa.schema.CreateSequence(sa.Sequence("sprint_number_seq")))  # §9
    op.create_table('sprint_repositories',
    sa.Column('sprint_number', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('repository_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['repository_id'], ['repositories.repository_id'], ),
    sa.ForeignKeyConstraint(['sprint_number'], ['sprints.sprint_number'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('sprint_number', 'repository_id')
    )
    op.create_table('sprint_issues',
    sa.Column('sprint_number', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('issue_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['issue_id'], ['issues.issue_id'], ),
    sa.ForeignKeyConstraint(['sprint_number'], ['sprints.sprint_number'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('sprint_number', 'issue_id')
    )
    op.create_table('sprint_resumes',
    sa.Column('resume_id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('sprint_number', sa.Integer(), nullable=False),
    sa.Column('selected_step', sa.Text(), nullable=False),
    sa.Column('selected_why', sa.Text(), nullable=False),
    sa.Column('rejected_alternatives', sa.Text(), nullable=False),
    sa.Column('current_task', sa.Text(), nullable=False),
    sa.Column('dod_state', sa.Text(), nullable=False),
    sa.Column('next_safe_step', sa.Text(), nullable=False),
    sa.Column('recorded_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['sprint_number'], ['sprints.sprint_number'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('resume_id'),
    sa.UniqueConstraint('resume_id', 'sprint_number')
    )

    # --- §3.4 Budget
    op.create_table('sprint_budget_events',
    sa.Column('budget_event_id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('sprint_number', sa.Integer(), nullable=False),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('charged', sa.Boolean(), nullable=False),
    sa.Column('task_ref', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('request_id', sa.Text(), nullable=False),
    sa.Column('occurred_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint("charged = (event_type <> 'infrastructure_blocked')", name='budget_charge_matches_type'),
    sa.CheckConstraint("event_type IN ('red_review','blocked','red_ci','preempt','recreated_task','hotfix','infrastructure_blocked')"),
    sa.ForeignKeyConstraint(['sprint_number'], ['sprints.sprint_number'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('budget_event_id'),
    sa.UniqueConstraint('request_id')
    )

    # --- §3.5 Cards
    op.create_table('tasks',
    sa.Column('task_ref', sa.Text(), nullable=False),
    sa.Column('project_id', sa.Text(), nullable=False),
    sa.Column('task_number', sa.Integer(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), server_default=sa.text("''"), nullable=False),
    sa.Column('task_type', sa.Text(), nullable=False),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('archived', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('position', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('sprint_number', sa.Integer(), nullable=True),
    sa.Column('claim_worker', sa.Text(), nullable=True),
    sa.Column('claimed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('slug', sa.Text(), nullable=True),
    sa.Column('base_branch', sa.Text(), nullable=True),
    sa.Column('seed_ref', sa.Text(), nullable=True),
    sa.Column('complexity', sa.Text(), server_default=sa.text("'standard'"), nullable=False),
    sa.Column('family_preference', sa.Text(), server_default=sa.text("'auto'"), nullable=False),
    sa.Column('head_override', sa.Text(), nullable=True),
    sa.Column('review_head_override', sa.Text(), nullable=True),
    sa.Column('resolved_worker_head', sa.Text(), nullable=True),
    sa.Column('resolved_worker_family', sa.Text(), nullable=True),
    sa.Column('resolved_review_head', sa.Text(), nullable=True),
    sa.Column('resolved_review_family', sa.Text(), nullable=True),
    sa.Column('routing_reason', sa.Text(), nullable=True),
    sa.Column('quota_snapshot_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.Column('codex_launch_mode', sa.Text(), nullable=True),
    sa.Column('retry_same', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('retry_switch', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('extensions', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint("codex_launch_mode IN ('tui')"),
    sa.CheckConstraint("complexity IN ('cheap','standard','hard','frontier')"),
    sa.CheckConstraint("family_preference IN ('auto','claude','codex')"),
    sa.CheckConstraint("state IN ('issues','ready','in_progress','validate','assessment','blocked','done')"),
    sa.CheckConstraint("task_type IN ('code','research')"),
    sa.CheckConstraint("title <> ''"),
    sa.CheckConstraint('retry_same >= 0'),
    sa.CheckConstraint('retry_switch >= 0'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.ForeignKeyConstraint(['sprint_number'], ['sprints.sprint_number'], ),
    sa.PrimaryKeyConstraint('task_ref'),
    sa.UniqueConstraint('project_id', 'task_number'),
    sa.UniqueConstraint('task_ref', 'sprint_number')
    )
    op.create_table('task_retry_heads',
    sa.Column('task_ref', sa.Text(), nullable=False),
    sa.Column('ordinal', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('head', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['task_ref'], ['tasks.task_ref'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_ref', 'ordinal')
    )
    op.create_table('task_issues',
    sa.Column('task_ref', sa.Text(), nullable=False),
    sa.Column('issue_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['issue_id'], ['issues.issue_id'], ),
    sa.ForeignKeyConstraint(['task_ref'], ['tasks.task_ref'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_ref', 'issue_id')
    )
    op.create_table('task_dependencies',
    sa.Column('task_ref', sa.Text(), nullable=False),
    sa.Column('depends_on', sa.Text(), nullable=False),
    sa.CheckConstraint('task_ref <> depends_on', name='no_self_dependency'),
    sa.ForeignKeyConstraint(['depends_on'], ['tasks.task_ref'], ),
    sa.ForeignKeyConstraint(['task_ref'], ['tasks.task_ref'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_ref', 'depends_on')
    )
    op.create_table('task_supersessions',
    sa.Column('task_ref', sa.Text(), nullable=False),
    sa.Column('supersedes', sa.Text(), nullable=False),
    sa.Column('recorded_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint('task_ref <> supersedes', name='no_self_supersession'),
    sa.ForeignKeyConstraint(['supersedes'], ['tasks.task_ref'], ),
    sa.ForeignKeyConstraint(['task_ref'], ['tasks.task_ref'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_ref')
    )

    # --- §3.6 Reservations
    op.create_table('sprint_projects',
    sa.Column('sprint_number', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('project_id', sa.Text(), nullable=False),
    sa.Column('reserved', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('reserved_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('released_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.CheckConstraint('reserved = (released_at IS NULL)', name='reserved_matches_release'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ),
    sa.ForeignKeyConstraint(['sprint_number'], ['sprints.sprint_number'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('sprint_number', 'project_id')
    )
    op.create_index('sprint_projects_one_live_reservation', 'sprint_projects', ['project_id'], unique=True, postgresql_where=sa.text('reserved'))

    # --- §3.7 Comments
    op.create_table('sprint_comments',
    sa.Column('comment_id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('sprint_number', sa.Integer(), nullable=False),
    sa.Column('marker', sa.Text(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('actor_role', sa.Text(), nullable=True),
    sa.Column('actor_id', sa.Text(), nullable=True),
    sa.Column('request_id', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['sprint_number'], ['sprints.sprint_number'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('comment_id'),
    sa.UniqueConstraint('request_id')
    )
    op.create_table('task_comments',
    sa.Column('comment_id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('task_ref', sa.Text(), nullable=False),
    sa.Column('marker', sa.Text(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('actor_role', sa.Text(), nullable=True),
    sa.Column('actor_id', sa.Text(), nullable=True),
    sa.Column('request_id', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_ref'], ['tasks.task_ref'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('comment_id'),
    sa.UniqueConstraint('request_id')
    )
    op.create_index('task_comments_by_task', 'task_comments', ['task_ref', 'created_at'], unique=False)

    # --- §3.8 Sprint decisions
    op.create_table('sprint_decisions',
    sa.Column('decision_id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('sprint_number', sa.Integer(), nullable=False),
    sa.Column('subject_kind', sa.Text(), nullable=False),
    sa.Column('issue_id', sa.Text(), nullable=True),
    sa.Column('task_ref', sa.Text(), nullable=True),
    sa.Column('verdict', sa.Text(), nullable=False),
    sa.Column('actual', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('request_id', sa.Text(), nullable=False),
    sa.Column('decided_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.CheckConstraint("(subject_kind = 'issue' AND issue_id IS NOT NULL AND task_ref IS NULL) OR (subject_kind = 'card'  AND task_ref IS NOT NULL AND issue_id IS NULL)", name='decision_subject_is_exactly_one'),
    sa.CheckConstraint("(subject_kind = 'issue' AND verdict IN ('resolved','invalid','duplicate','wont_do','open','already_closed')) OR (subject_kind = 'card'  AND verdict IN ('done','drop','already_moved'))", name='decision_verdict_in_vocabulary'),
    sa.CheckConstraint("(verdict IN ('already_closed','already_moved')) = (actual IS NOT NULL)", name='decision_actual_only_on_confirmation'),
    sa.CheckConstraint("reason <> ''"),
    sa.CheckConstraint("subject_kind IN ('issue','card')"),
    sa.ForeignKeyConstraint(['sprint_number'], ['sprints.sprint_number'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('decision_id')
    )
    op.create_index('sprint_decisions_one_per_card', 'sprint_decisions', ['sprint_number', 'task_ref'], unique=True, postgresql_where=sa.text('task_ref IS NOT NULL'))
    op.create_index('sprint_decisions_one_per_issue', 'sprint_decisions', ['sprint_number', 'issue_id'], unique=True, postgresql_where=sa.text('issue_id IS NOT NULL'))

    # --- §3.9 Request ownership, events and idempotency
    op.create_table('requests',
    sa.Column('request_id', sa.Text(), nullable=False),
    sa.Column('operation', sa.Text(), nullable=False),
    sa.Column('intent', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('protocol', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('entity_kind', sa.Text(), nullable=True),
    sa.Column('ref', sa.Text(), nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('settled_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.CheckConstraint("(status = 'staged') = (settled_at IS NULL)", name='request_settled_matches_status'),
    sa.CheckConstraint("entity_kind IN ('product','issue','sprint','card')"),
    sa.CheckConstraint("status IN ('staged','committed','discarded')"),
    sa.PrimaryKeyConstraint('request_id'),
    sa.UniqueConstraint('request_id', 'ref', name='requests_ref_identity')
    )
    op.create_table('board_events',
    sa.Column('event_id', sa.Text(), nullable=False),
    sa.Column('request_id', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('entity_kind', sa.Text(), nullable=False),
    sa.Column('ref', sa.Text(), nullable=False),
    sa.Column('actor_role', sa.Text(), nullable=False),
    sa.Column('actor_id', sa.Text(), nullable=False),
    sa.Column('head_run_ref', sa.Text(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('source_state', sa.Text(), nullable=True),
    sa.Column('target_state', sa.Text(), nullable=True),
    sa.Column('related_refs', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('occurred_at', postgresql.TIMESTAMP(timezone=True), nullable=False),
    sa.Column('committed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('committed_at', postgresql.TIMESTAMP(timezone=True), nullable=True),
    sa.CheckConstraint("entity_kind IN ('product','issue','sprint','card')"),
    sa.CheckConstraint("kind IN ('entity.created','entity.updated','product.archived','issue.closed','sprint.closed','sprint.stopped','sprint.reopened','card.readied','card.started','card.submitted','card.assessed','card.reworked','card.released','card.blocked','card.unblocked','card.returned','card.moved','card.reported','card.verdict','card.decided','card.decision_refused','attempt.usage','attempt.outcome')"),
    sa.ForeignKeyConstraint(['request_id'], ['requests.request_id'], ),
    sa.PrimaryKeyConstraint('event_id'),
    sa.UniqueConstraint('request_id')
    )
    op.create_index('board_events_by_ref', 'board_events', ['ref', 'occurred_at'], unique=False)
    # === Step 2: the constraints §3.13 defers, in the same reading order. ===

    # §3.3: both sprint cursors are scoped to this sprint by composite foreign key, and both are
    # DEFERRABLE INITIALLY DEFERRED because a sprint and the card it points at are written in one
    # transaction.
    op.create_foreign_key(
        "sprint_current_task_is_in_this_sprint",
        "sprints",
        "tasks",
        ["current_task_ref", "sprint_number"],
        ["task_ref", "sprint_number"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "sprint_resume_is_of_this_sprint",
        "sprints",
        "sprint_resumes",
        ["resume_id", "sprint_number"],
        ["resume_id", "sprint_number"],
        deferrable=True,
        initially="DEFERRED",
    )

    # §3.4
    op.create_foreign_key(
        "budget_card_is_in_this_sprint",
        "sprint_budget_events",
        "tasks",
        ["task_ref", "sprint_number"],
        ["task_ref", "sprint_number"],
    )

    # §3.8
    op.create_foreign_key(
        "decided_issue_is_declared_by_this_sprint",
        "sprint_decisions",
        "sprint_issues",
        ["sprint_number", "issue_id"],
        ["sprint_number", "issue_id"],
    )
    op.create_foreign_key(
        "decided_card_is_in_this_sprint",
        "sprint_decisions",
        "tasks",
        ["task_ref", "sprint_number"],
        ["task_ref", "sprint_number"],
    )

    # §3.9: the generated sprint ref each composite claim key joins on.
    for table in ("sprint_comments", "sprint_budget_events", "sprint_decisions"):
        op.add_column(table, sa.Column("sprint_ref", sa.Text(), SPRINT_REF, nullable=True))

    # §3.9: the five request_id foreign keys.  Each child claims its request *and* agrees with it
    # about which entity the request was claimed for.
    op.create_foreign_key(
        "board_event_claims_its_request",
        "board_events",
        "requests",
        ["request_id", "ref"],
        ["request_id", "ref"],
    )
    op.create_foreign_key(
        "task_comment_claims_its_request",
        "task_comments",
        "requests",
        ["request_id", "task_ref"],
        ["request_id", "ref"],
    )
    op.create_foreign_key(
        "sprint_comment_claims_its_request",
        "sprint_comments",
        "requests",
        ["request_id", "sprint_ref"],
        ["request_id", "ref"],
    )
    op.create_foreign_key(
        "budget_event_claims_its_request",
        "sprint_budget_events",
        "requests",
        ["request_id", "sprint_ref"],
        ["request_id", "ref"],
    )
    op.create_foreign_key(
        "decision_belongs_to_its_close_request",
        "sprint_decisions",
        "requests",
        ["request_id", "sprint_ref"],
        ["request_id", "ref"],
    )

    # === §5.5: the two non-owner roles, their grants and their default privileges. ===
    _roles_and_grants()


def _roles_and_grants() -> None:
    """§5.5's fence, with the two generated passwords supplied by the run.

    PostgreSQL takes no bound parameter in `CREATE ROLE`, so the password is rendered as a SQL
    literal by the dialect's own literal processor — the same quoting `psycopg.sql.Literal`
    produces — and the statement is sent through `exec_driver_sql`, which does not reinterpret
    `:` or `%` in it the way a `text()` construct would.
    """
    bind = op.get_bind()
    attributes = op.get_context().config.attributes
    passwords = attributes.get("passwords") or {}
    missing = [name for name in PASSWORD_PARAMETERS if not passwords.get(name)]
    if missing:
        raise RuntimeError(
            "the initial board-store revision needs the generated "
            + ", ".join(missing)
            + "; §5.5 creates the two non-owner roles and this run was given none"
        )
    quote = sa.Text().literal_processor(dialect=bind.dialect)
    app_password = quote(passwords["app_password"])
    read_password = quote(passwords["read_password"])

    if attributes.get("reuse_existing_roles"):
        rows = bind.exec_driver_sql(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname IN (%s, %s)",
            (APP_ROLE, READ_ROLE),
        ).fetchall()
        expected = {(APP_ROLE, True, False, False, False), (READ_ROLE, True, False, False, False)}
        if set(rows) != expected:
            raise RuntimeError("the successor database requires the existing app/read role boundary")
    else:
        bind.exec_driver_sql(f"CREATE ROLE {APP_ROLE}  LOGIN PASSWORD {app_password}")
        bind.exec_driver_sql(f"CREATE ROLE {READ_ROLE} LOGIN PASSWORD {read_password}")
    for statement in (
        f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}, {READ_ROLE}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}",
        f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READ_ROLE}",
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA public "
            f"GRANT USAGE ON SEQUENCES TO {APP_ROLE}"
        ),
        (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA public "
            f"GRANT SELECT ON TABLES TO {READ_ROLE}"
        ),
    ):
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    """There is none, and §7.4's reason is PostgreSQL's transactional DDL.

    A revision that fails leaves the schema it was moving from, not half of one, so the recovery
    from a failed migration is to fix the revision and run it again — not to unwind a database
    that never changed.  Dropping the initial schema would also drop the board.
    """
    raise NotImplementedError(
        "the board store has no down migration: a failed revision rolls back whole (§7.4), and "
        "unwinding the initial schema would delete the board"
    )
