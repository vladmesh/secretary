-- Migration 0001: the initial board-store schema.
--
-- Every statement below is transcribed verbatim from docs/BOARD_STORE.md; the source line of
-- each fence is named above it so a reader can diff the two rather than trust this file. The
-- order is §3.13's: step 1 creates the tables in §3 reading order plus §7.4's schema_migrations,
-- step 2 adds the constraints whose targets are forward or mutual references. §5.5's roles come
-- last, so its `GRANT ... ON ALL TABLES` reaches the 22 tables created above and its
-- `ALTER DEFAULT PRIVILEGES` reaches the tables every later migration adds.
--
-- The two password markers in the §5.5 fence are the runner's parameters (§7.4): the generated
-- passwords are substituted as SQL literals at apply time and are never bytes of this file, so
-- the file — and therefore its checksum — is identical on every installation.

-- Step 1: create tables, in §3 reading order (§3.13).

-- docs/BOARD_STORE.md:444
CREATE TABLE products (
    product_id   text PRIMARY KEY,                    -- "secretary"; matches ^[a-z0-9][a-z0-9-]{0,62}$
    ref          text GENERATED ALWAYS AS ('product:' || product_id) STORED UNIQUE,
    title        text NOT NULL CHECK (title <> ''),
    description  text NOT NULL DEFAULT '',
    state        text NOT NULL DEFAULT 'active' CHECK (state IN ('active','archived')),
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL
);

CREATE TABLE projects (
    project_id       text PRIMARY KEY,                -- registry id, e.g. "secretary"
    enabled          boolean NOT NULL DEFAULT true,
    plane            text NOT NULL DEFAULT 'project',
    adapter          text,
    orca_binding     text,
    registry_present boolean NOT NULL DEFAULT true    -- false: referenced by history, absent from the registry
);

CREATE TABLE repositories (
    repository_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id     text REFERENCES projects(project_id),
    path           text NOT NULL,                     -- absolute working-tree path
    remote         text,
    default_branch text NOT NULL DEFAULT 'main',
    role           text NOT NULL DEFAULT 'primary'
                     CHECK (role IN ('primary','curator_root')),
    UNIQUE (path)
);
CREATE UNIQUE INDEX repositories_one_primary
    ON repositories (project_id) WHERE role = 'primary';

CREATE TABLE product_projects (                       -- Product.projects, today a JSON array
    product_id text NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    project_id text NOT NULL REFERENCES projects(project_id),
    PRIMARY KEY (product_id, project_id)
);

-- docs/BOARD_STORE.md:541
CREATE TABLE issues (
    issue_id     text PRIMARY KEY,                    -- the 20-hex suffix of issue:<id>
    ref          text GENERATED ALWAYS AS ('issue:' || issue_id) STORED UNIQUE,
    product_id   text NOT NULL REFERENCES products(product_id),
    title        text NOT NULL CHECK (title <> ''),
    description  text NOT NULL DEFAULT '',
    issue_kind   text NOT NULL
                   CHECK (issue_kind IN ('bug','feature','question','improvement')),
    priority     text NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open','closed')),
    close_reason text CHECK (close_reason IN ('resolved','invalid','duplicate','wont_do')),
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    CONSTRAINT issue_close_reason_matches_state
        CHECK ((state = 'closed') = (close_reason IS NOT NULL))
);

-- docs/BOARD_STORE.md:565
CREATE TABLE sprints (
    sprint_number      integer PRIMARY KEY,           -- N in sprint:N
    ref                text GENERATED ALWAYS AS ('sprint:' || sprint_number) STORED UNIQUE,
    goal               text NOT NULL,
    definition_of_done text NOT NULL,
    product_id         text REFERENCES products(product_id),
    status             text NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','closed','stopped')),
    observer           jsonb,                         -- (J1)
    worker_pin         text,
    reviewer_pin       text,
    -- Both cursors are scoped to this sprint by composite foreign key, not by a bare
    -- existence check.  See "Scoped relations" below.
    current_task_ref   text,
    resume_id          bigint,
    close_reason       text,
    closeout_document  text,                          -- state/knowledge path, not the prose
    source_audit       jsonb,                         -- (J2)
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL,
    closed_at          timestamptz,
    CONSTRAINT sprint_closed_has_time CHECK ((status = 'open') = (closed_at IS NULL))
);
CREATE SEQUENCE sprint_number_seq;                    -- see §9

CREATE TABLE sprint_repositories (
    sprint_number integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    repository_id bigint  NOT NULL REFERENCES repositories(repository_id),
    PRIMARY KEY (sprint_number, repository_id)
);

CREATE TABLE sprint_issues (
    sprint_number integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    issue_id      text    NOT NULL REFERENCES issues(issue_id),
    PRIMARY KEY (sprint_number, issue_id)
);

CREATE TABLE sprint_resumes (                         -- append-only; sprints.resume_id names the live one
    resume_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_number        integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    selected_step        text NOT NULL,
    selected_why         text NOT NULL,
    rejected_alternatives text NOT NULL,
    current_task         text NOT NULL,
    dod_state            text NOT NULL,
    next_safe_step       text NOT NULL,
    recorded_at          timestamptz NOT NULL,
    -- The target a scoped foreign key needs; redundant with the primary key by design.
    UNIQUE (resume_id, sprint_number)
);

-- docs/BOARD_STORE.md:668
CREATE TABLE sprint_budget_events (
    budget_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_number   integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    event_type      text NOT NULL CHECK (event_type IN
        ('red_review','blocked','red_ci','preempt','recreated_task','hotfix',
         'infrastructure_blocked')),
    charged         boolean NOT NULL,
    task_ref        text,
    reason          text NOT NULL,
    request_id      text NOT NULL UNIQUE,            -- references `requests`; see §3.9
    occurred_at     timestamptz NOT NULL,
    CONSTRAINT budget_charge_matches_type
        CHECK (charged = (event_type <> 'infrastructure_blocked'))
);

-- docs/BOARD_STORE.md:708
CREATE TABLE tasks (
    task_ref       text PRIMARY KEY,                  -- "secretary-1580"
    project_id     text NOT NULL REFERENCES projects(project_id),
    task_number    integer NOT NULL,
    title          text NOT NULL CHECK (title <> ''),
    description    text NOT NULL DEFAULT '',
    task_type      text NOT NULL CHECK (task_type IN ('code','research')),
    state          text NOT NULL CHECK (state IN
                     ('issues','ready','in_progress','validate','assessment','blocked','done')),
    archived       boolean NOT NULL DEFAULT false,
    position       integer NOT NULL DEFAULT 0,
    sprint_number  integer REFERENCES sprints(sprint_number),
    claim_worker   text,
    claimed_at     timestamptz,
    -- workspace
    slug           text,
    base_branch    text,
    seed_ref       text,
    -- routing
    complexity        text NOT NULL DEFAULT 'standard'
                        CHECK (complexity IN ('cheap','standard','hard','frontier')),
    family_preference text NOT NULL DEFAULT 'auto'
                        CHECK (family_preference IN ('auto','claude','codex')),
    head_override        text,
    review_head_override text,
    resolved_worker_head text,
    resolved_worker_family text,
    resolved_review_head text,
    resolved_review_family text,
    routing_reason       text,
    quota_snapshot_at    timestamptz,
    codex_launch_mode    text CHECK (codex_launch_mode IN ('tui')),
    retry_same     integer NOT NULL DEFAULT 0 CHECK (retry_same >= 0),
    retry_switch   integer NOT NULL DEFAULT 0 CHECK (retry_switch >= 0),
    extensions     jsonb NOT NULL DEFAULT '{}'::jsonb,   -- (J3)
    created_at     timestamptz NOT NULL,
    updated_at     timestamptz NOT NULL,
    UNIQUE (project_id, task_number),
    -- The target the sprint's scoped cursor and decision keys need (§3.3, §3.8).
    -- Redundant with the primary key by design.
    UNIQUE (task_ref, sprint_number)
);

CREATE TABLE task_retry_heads (                       -- retry_heads, today a delimited string
    task_ref text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    ordinal  integer NOT NULL,
    head     text NOT NULL,
    PRIMARY KEY (task_ref, ordinal)
);

CREATE TABLE task_issues (
    task_ref text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    issue_id text NOT NULL REFERENCES issues(issue_id),
    PRIMARY KEY (task_ref, issue_id)
);

CREATE TABLE task_dependencies (                      -- blocked_by
    task_ref    text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    depends_on  text NOT NULL REFERENCES tasks(task_ref),
    PRIMARY KEY (task_ref, depends_on),
    CONSTRAINT no_self_dependency CHECK (task_ref <> depends_on)
);

CREATE TABLE task_supersessions (                     -- supersedes
    task_ref     text PRIMARY KEY REFERENCES tasks(task_ref) ON DELETE CASCADE,
    supersedes   text NOT NULL REFERENCES tasks(task_ref),
    recorded_at  timestamptz NOT NULL,
    CONSTRAINT no_self_supersession CHECK (task_ref <> supersedes)
);

-- docs/BOARD_STORE.md:788
CREATE TABLE sprint_projects (
    sprint_number integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    project_id    text    NOT NULL REFERENCES projects(project_id),
    reserved      boolean NOT NULL DEFAULT true,
    reserved_at   timestamptz NOT NULL,
    released_at   timestamptz,
    PRIMARY KEY (sprint_number, project_id),
    CONSTRAINT reserved_matches_release CHECK (reserved = (released_at IS NULL))
);

CREATE UNIQUE INDEX sprint_projects_one_live_reservation
    ON sprint_projects (project_id) WHERE reserved;

-- docs/BOARD_STORE.md:807
CREATE TABLE sprint_comments (
    comment_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_number integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    marker        text,                               -- "po", "sprint:resume", NULL for unmarked
    body          text NOT NULL,
    actor_role    text,
    actor_id      text,
    -- Unique *per claimed request*; the namespace itself is `requests` (§3.9), which is
    -- created later, so the foreign key is a deferred constraint added in §3.13 step 2.
    request_id    text UNIQUE,
    created_at    timestamptz NOT NULL
);

CREATE TABLE task_comments (
    comment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_ref    text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    marker      text,                                 -- role, or report:/review:/decision:*
    body        text NOT NULL,
    actor_role  text,
    actor_id    text,
    request_id  text UNIQUE,                          -- FK added in §3.13 step 2
    created_at  timestamptz NOT NULL
);
CREATE INDEX task_comments_by_task ON task_comments (task_ref, created_at);

-- docs/BOARD_STORE.md:841
CREATE TABLE sprint_decisions (
    decision_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_number integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    subject_kind  text NOT NULL CHECK (subject_kind IN ('issue','card')),
    -- Both subjects are scoped to this sprint below; neither is a bare existence check.
    issue_id      text,
    task_ref      text,
    verdict       text NOT NULL,
    actual        text,
    reason        text NOT NULL CHECK (reason <> ''),
    -- References `requests`, deliberately NOT unique: one close claims one id and
    -- writes one decision per declared issue and per remaining card (§3.9).
    request_id    text NOT NULL,
    decided_at    timestamptz NOT NULL,
    CONSTRAINT decision_subject_is_exactly_one CHECK (
        (subject_kind = 'issue' AND issue_id IS NOT NULL AND task_ref IS NULL) OR
        (subject_kind = 'card'  AND task_ref IS NOT NULL AND issue_id IS NULL)),
    CONSTRAINT decision_verdict_in_vocabulary CHECK (
        (subject_kind = 'issue' AND verdict IN
            ('resolved','invalid','duplicate','wont_do','open','already_closed')) OR
        (subject_kind = 'card'  AND verdict IN ('done','drop','already_moved'))),
    CONSTRAINT decision_actual_only_on_confirmation CHECK (
        (verdict IN ('already_closed','already_moved')) = (actual IS NOT NULL))
);
CREATE UNIQUE INDEX sprint_decisions_one_per_issue
    ON sprint_decisions (sprint_number, issue_id) WHERE issue_id IS NOT NULL;
CREATE UNIQUE INDEX sprint_decisions_one_per_card
    ON sprint_decisions (sprint_number, task_ref) WHERE task_ref IS NOT NULL;

-- docs/BOARD_STORE.md:918
CREATE TABLE requests (
    request_id  text PRIMARY KEY,                     -- the whole installation's one namespace
    operation   text NOT NULL,                        -- "card.comment", "sprint.close", …
    intent      jsonb NOT NULL,                       -- (J5) frozen at claim, never updated
    status      text NOT NULL CHECK (status IN ('staged','committed','discarded')),
    -- A protocol occurrence may never be replaced by a generic stage; see the rule below.
    protocol    boolean NOT NULL DEFAULT false,
    entity_kind text CHECK (entity_kind IN ('product','issue','sprint','card')),
    ref         text,
    created_at  timestamptz NOT NULL,
    settled_at  timestamptz,
    CONSTRAINT request_settled_matches_status
        CHECK ((status = 'staged') = (settled_at IS NULL)),
    -- The target of every child's composite claim key below.  Redundant with the
    -- primary key by design, exactly as the scoped sprint keys of §3.3 are.
    CONSTRAINT requests_ref_identity UNIQUE (request_id, ref)
);

CREATE TABLE board_events (
    event_id     text PRIMARY KEY,                    -- globally unique, as TaskAudit.event_id_owner requires
    request_id   text NOT NULL UNIQUE REFERENCES requests(request_id),
    kind         text NOT NULL CHECK (kind IN (       -- EventKind, §3.12
        'entity.created','entity.updated','product.archived','issue.closed',
        'sprint.closed','sprint.stopped','sprint.reopened',
        'card.readied','card.started','card.submitted','card.assessed','card.reworked',
        'card.released','card.blocked','card.unblocked','card.returned','card.moved',
        'card.reported','card.verdict','card.decided','card.decision_refused',
        'attempt.usage','attempt.outcome')),
    entity_kind  text NOT NULL CHECK (entity_kind IN ('product','issue','sprint','card')),
    ref          text NOT NULL,
    actor_role   text NOT NULL,
    actor_id     text NOT NULL,
    head_run_ref text,
    reason       text NOT NULL,
    source_state text,
    target_state text,
    related_refs text[] NOT NULL DEFAULT '{}',
    data         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- (J4)
    occurred_at  timestamptz NOT NULL,
    committed    boolean NOT NULL DEFAULT false,
    committed_at timestamptz
);
CREATE INDEX board_events_by_ref ON board_events (ref, occurred_at);

-- docs/BOARD_STORE.md:1842 (§7.4)
CREATE TABLE schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    applied_at  timestamptz NOT NULL,
    checksum    text NOT NULL
);

-- Step 2: the deferred constraints, in the same reading order (§3.13).

-- docs/BOARD_STORE.md:635
ALTER TABLE sprints
  ADD CONSTRAINT sprint_current_task_is_in_this_sprint
      FOREIGN KEY (current_task_ref, sprint_number)
      REFERENCES tasks (task_ref, sprint_number) MATCH SIMPLE
      DEFERRABLE INITIALLY DEFERRED,
  ADD CONSTRAINT sprint_resume_is_of_this_sprint
      FOREIGN KEY (resume_id, sprint_number)
      REFERENCES sprint_resumes (resume_id, sprint_number) MATCH SIMPLE
      DEFERRABLE INITIALLY DEFERRED;

-- docs/BOARD_STORE.md:687
ALTER TABLE sprint_budget_events
  ADD CONSTRAINT budget_card_is_in_this_sprint
      FOREIGN KEY (task_ref, sprint_number)
      REFERENCES tasks (task_ref, sprint_number) MATCH SIMPLE;

-- docs/BOARD_STORE.md:874
ALTER TABLE sprint_decisions
  ADD CONSTRAINT decided_issue_is_declared_by_this_sprint
      FOREIGN KEY (sprint_number, issue_id)
      REFERENCES sprint_issues (sprint_number, issue_id) MATCH SIMPLE,
  ADD CONSTRAINT decided_card_is_in_this_sprint
      FOREIGN KEY (task_ref, sprint_number)
      REFERENCES tasks (task_ref, sprint_number) MATCH SIMPLE;

-- docs/BOARD_STORE.md:969
ALTER TABLE sprint_comments
  ADD COLUMN sprint_ref text GENERATED ALWAYS AS ('sprint:' || sprint_number) STORED;
ALTER TABLE sprint_budget_events
  ADD COLUMN sprint_ref text GENERATED ALWAYS AS ('sprint:' || sprint_number) STORED;
ALTER TABLE sprint_decisions
  ADD COLUMN sprint_ref text GENERATED ALWAYS AS ('sprint:' || sprint_number) STORED;

-- docs/BOARD_STORE.md:978
ALTER TABLE board_events
  ADD CONSTRAINT board_event_claims_its_request
      FOREIGN KEY (request_id, ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE task_comments
  ADD CONSTRAINT task_comment_claims_its_request
      FOREIGN KEY (request_id, task_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE sprint_comments
  ADD CONSTRAINT sprint_comment_claims_its_request
      FOREIGN KEY (request_id, sprint_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE sprint_budget_events
  ADD CONSTRAINT budget_event_claims_its_request
      FOREIGN KEY (request_id, sprint_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE sprint_decisions
  ADD CONSTRAINT decision_belongs_to_its_close_request
      FOREIGN KEY (request_id, sprint_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;

-- §5.5: the two non-owner roles, their grants and their default privileges.
-- docs/BOARD_STORE.md:1455
CREATE ROLE secretary_app  LOGIN PASSWORD :'app_password';
CREATE ROLE secretary_read LOGIN PASSWORD :'read_password';
GRANT USAGE ON SCHEMA public TO secretary_app, secretary_read;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO secretary_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO secretary_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO secretary_read;
ALTER DEFAULT PRIVILEGES FOR ROLE secretary_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO secretary_app;
ALTER DEFAULT PRIVILEGES FOR ROLE secretary_owner IN SCHEMA public
  GRANT USAGE ON SEQUENCES TO secretary_app;
ALTER DEFAULT PRIVILEGES FOR ROLE secretary_owner IN SCHEMA public
  GRANT SELECT ON TABLES TO secretary_read;
