# The PostgreSQL board store

Technical reference for the board store: schema, backend selection, transactions, audit mapping,
migrations, import mechanics, identifiers and store configuration.

The production installation serves Products, Issues, Sprints and Cards from PostgreSQL
(`SECRETARY_CARD_BACKEND=postgres`). The Kanboard implementation stays in the code for explicit
rollback (`SECRETARY_CARD_BACKEND=kanboard`); the old Kanboard store is kept as a read-only archive.
A missing or empty selector is a configuration error rather than an implicit rollback.

Related documents:

- module layout and component boundaries: [ARCHITECTURE.md](ARCHITECTURE.md), incl.
  [Cutover controller](ARCHITECTURE.md#cutover-controller);
- `secretary cutover` and the audit journal import contracts:
  [PROTOCOLS.md](PROTOCOLS.md#secretary-cutover),
  [Audit journal import protocol](PROTOCOLS.md#audit-journal-import-protocol);
- provisioning, import rehearsal and cutover runbooks:
  [PostgreSQL board store](OPERATIONS.md#postgresql-board-store),
  [Rehearsing the complete board import](OPERATIONS.md#rehearsing-the-complete-board-import),
  [PostgreSQL board-store cutover](OPERATIONS.md#postgresql-board-store-cutover);
- backup and restore of the store: [Backend-aware cold archives](RECOVERY.md#backend-aware-cold-archives),
  [Cutover controller state](RECOVERY.md#cutover-controller-state).

---

## 1. Scope

The schema holds board entities only: Products, Issues, Sprints, Cards, their links, comments,
budget events, close decisions, request claims and typed board events. Everything else stays
outside it (§3.11).

---

## 2. Store modules and backend selection

### 2.1 Modules

| Module | Role |
|---|---|
| `board/schema.py` | SQLAlchemy models; the source of truth for §3 |
| `board/migrations/` | Alembic environment and revisions `0001`–`0007` (§7.4) |
| `board/migrate.py` | migration runner: advisory lock, owner connection, role passwords (§7.4) |
| `board/store.py` | `board-store.env` parsing, resolution and git exclusion (§5.4) |
| `board/provision.py` | Compose definition, container/volume reconciliation, role verification (§5.1–§5.5) |
| `board/backend.py` | backend selector, `board_client`, entity identities and record keys (§2.2) |
| `board/sql_cards.py` | `SqlCardClient`: the board vocabulary over cards (§7.1) |
| `board/sql_product_issues.py`, `board/sql_sprints.py` | Product/Issue and Sprint rows over the same client |
| `board/sql_audit.py` | `SqlTaskAudit`: the `TaskAudit` contract over `requests`/`board_events` (§7.3) |
| `board/import_board.py` | Kanboard → PostgreSQL importer and parity (§8) |
| `board/postgres_recovery.py` | PostgreSQL archive restore helpers (see RECOVERY.md) |

### 2.2 Backend selection and client construction

`TaskReader`/`TaskWriter`, `SprintReader`/`SprintWriter` and `ProductIssueStore` each have a
Kanboard JSON-RPC implementation and a PostgreSQL one. The process chooses between them with
`SECRETARY_CARD_BACKEND`:

- values `kanboard` or `postgres`; missing, empty and unknown values refuse;
- read once per process (`card_backend()` caches it);
- set in protected `<instance>/runtime.env`; the CLI loads it from there, the dispatcher and web
  units read the same file, and the role environment allowlist passes it to observer, worker,
  reviewer, steward, retro and curator processes;
- never inferred from `board-store.env`: that file provides connection material only;
- no dual write, no read fallback between backends;
- reported by `secretary status` as `card_backend`.

`board/backend.py:board_client(instance, serves=..., role="app")` is the only constructor of a
board client. `serves` names what the call site needs (`card`, `sprint`, `product/issue`); under
`postgres` an unknown capability refuses by name. Store and selector failures leave as
`TaskError` (`backend_error`, `backend_unavailable`), not tracebacks. Two modules build a Kanboard
client directly: `bootstrap.py` (creates the Kanboard board itself) and `board/import_board.py`
(reads the Kanboard source). `tests/test_architecture.py` enforces that list.

**Entity identity in normalized rows.** `<kind>_<backend>_<n>` (`task_kanboard_12`,
`task_postgres_37`, `sprint_kanboard_9`) is minted only by `entity_id` and read only by
`entity_number`, which accepts both backends and a bare number. `n` is Kanboard's task id or the
store's `board_key`. It is not the row's identity; the reference is (§9).

**Integer record keys.** The inherited board vocabulary addresses rows by integer. Each row stores
its key as a unique indexed `board_key`:

| Kind | Range | Allocation |
|---|---|---|
| Card | `[1, 2 000 000 000)` | `card_board_key_seq`, immutable |
| Sprint, numbered `sprint:N` | `[2 000 000 000, 2 500 000 000)` | base + N |
| Sprint, other references | `[2 500 000 000, 3 000 000 000)` | hash of the reference |
| Product | `[3 000 000 000, 4 000 000 000)` | hash of `product:<id>` |
| Issue | `[4 000 000 000, 5 000 000 000)` | hash of `issue:<id>` |

Lookups use the unique index. A malformed or colliding key is refused, never resolved by scanning;
keys cannot cross kinds.

---

## 3. ER schema

Conventions: `text` identifiers, `timestamptz` times, surrogate `bigint` keys only where a row has
no natural key. Every reference between entities is a foreign key; a reference meaningful only
within one sprint is a composite foreign key carrying the sprint (§3.3, §3.4, §3.8). Closed
vocabularies are `CHECK` constraints (§3.12). `jsonb` appears in seven columns (§3.10).

The DDL is grouped by entity. Forward and mutual references are added with `ALTER TABLE` after both
tables exist (§3.13). `board/schema.py` is authoritative; the DDL below mirrors it.

### 3.1 Products, projects, repositories

```sql
CREATE TABLE products (
    product_id   text PRIMARY KEY,                    -- "secretary"; matches ^[a-z0-9][a-z0-9-]{0,62}$
    board_key    bigint NOT NULL UNIQUE,              -- §2.2
    ref          text GENERATED ALWAYS AS ('product:' || product_id) STORED UNIQUE,
    title        text NOT NULL CHECK (title <> ''),
    description  text NOT NULL DEFAULT '',
    state        text NOT NULL DEFAULT 'active' CHECK (state IN ('active','archived')),
    extensions   jsonb NOT NULL DEFAULT '{}'::jsonb, -- (J7)
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

CREATE TABLE product_projects (
    product_id text NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    project_id text NOT NULL REFERENCES projects(project_id),
    PRIMARY KEY (product_id, project_id)
);
```

`products.extensions.kanboard` keeps metadata keys outside the known Product columns and the
`product_projects` set; known keys are filtered before merging, so provenance cannot override
identity or relationships.

`projects` and `repositories` exist so `tasks.project_id`, `sprint_projects.project_id` and
`sprint_repositories.repository_id` can be foreign keys. They are not canonical: the registry files
`<instance>/projects/*.yaml` are (§6.2), and `registered_projects()` reads the files. Writers:

- the importer creates rows from the registry and from every id or path history references (§8.3);
- a Product's project-set write inserts a missing `projects` row with the id only
  (`ON CONFLICT DO NOTHING`);
- a Sprint's repository-list write inserts a missing `repositories` row with the path only.

Rows are never deleted; `registry_present = false` marks an id absent from the registry.

### 3.2 Issues

```sql
CREATE TABLE issues (
    issue_id     text PRIMARY KEY,                    -- the 20-hex suffix of issue:<id>
    board_key    bigint NOT NULL UNIQUE,              -- §2.2
    ref          text GENERATED ALWAYS AS ('issue:' || issue_id) STORED UNIQUE,
    product_id   text NOT NULL REFERENCES products(product_id),
    title        text NOT NULL CHECK (title <> ''),
    description  text NOT NULL DEFAULT '',
    issue_kind   text NOT NULL
                   CHECK (issue_kind IN ('bug','feature','question','improvement')),
    priority     text NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open','closed')),
    close_reason text CHECK (close_reason IN ('resolved','invalid','duplicate','wont_do')),
    extensions   jsonb NOT NULL DEFAULT '{}'::jsonb, -- (J6); see §8.2
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    CONSTRAINT issue_close_reason_matches_state
        CHECK ((state = 'closed') = (close_reason IS NOT NULL))
);
```

### 3.3 Sprints

```sql
CREATE TABLE sprints (
    ref                text PRIMARY KEY,              -- "sprint:1037", "sprint:canary-terra-20260813"
    board_key          bigint NOT NULL UNIQUE,        -- §2.2
    sprint_number      integer UNIQUE,                -- N in sprint:N, NULL when the ref has none
    goal               text NOT NULL,
    definition_of_done text NOT NULL,
    product_id         text REFERENCES products(product_id),
    status             text NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open','closed','stopped')),
    observer           jsonb,                         -- (J1)
    worker_pin         text,
    reviewer_pin       text,
    current_task_ref   text,                          -- scoped cursor, below
    resume_id          bigint,                        -- scoped cursor, below
    close_reason       text,
    closeout_document  text,                          -- state/knowledge path, not the prose
    source_audit       jsonb,                         -- (J2)
    created_at         timestamptz NOT NULL,
    updated_at         timestamptz NOT NULL,
    closed_at          timestamptz,
    CONSTRAINT sprint_closed_has_time CHECK ((status = 'open') = (closed_at IS NULL)),
    CONSTRAINT sprint_ref_is_a_sprint_reference CHECK (ref ~ '^sprint:'),
    CONSTRAINT sprint_number_agrees_with_ref CHECK (
        (ref ~ '^sprint:[0-9]+$') = (sprint_number IS NOT NULL) AND
        (sprint_number IS NULL OR ref = 'sprint:' || sprint_number))
);
CREATE SEQUENCE sprint_number_seq;                    -- see §9

CREATE TABLE sprint_repositories (
    sprint_ref    text   NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    repository_id bigint NOT NULL REFERENCES repositories(repository_id),
    ordinal       integer NOT NULL DEFAULT 0,
    PRIMARY KEY (sprint_ref, repository_id)
);

CREATE TABLE sprint_issues (
    sprint_ref text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    issue_id   text NOT NULL REFERENCES issues(issue_id),
    ordinal    integer NOT NULL DEFAULT 0,
    PRIMARY KEY (sprint_ref, issue_id)
);

CREATE TABLE sprint_resumes (                         -- append-only; sprints.resume_id names the live one
    resume_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_ref           text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    selected_step        text NOT NULL,
    selected_why         text NOT NULL,
    rejected_alternatives text NOT NULL,
    current_task         text NOT NULL,
    dod_state            text NOT NULL,
    next_safe_step       text NOT NULL,
    recorded_at          timestamptz NOT NULL,
    recorded_at_source   text, -- malformed restored legacy spelling, retained as stale evidence
    UNIQUE (resume_id, sprint_ref)                    -- target of the scoped cursor
);
```

The reference is the sprint's identity, so non-numbered references are representable.
`sprint_number` is set exactly for `sprint:N` references. Resume fields are the six-field
`RESUME_FIELDS` tuple; resume freshness is derived, not stored.

**Scoped cursors.** A sprint's current task and live resume must belong to that sprint:

Deferred constraints (step 2 of §3.13):

```sql
ALTER TABLE sprints
  ADD CONSTRAINT sprint_current_task_is_in_this_sprint
      FOREIGN KEY (current_task_ref, ref)
      REFERENCES tasks (task_ref, sprint_ref) MATCH SIMPLE
      DEFERRABLE INITIALLY DEFERRED,
  ADD CONSTRAINT sprint_resume_is_of_this_sprint
      FOREIGN KEY (resume_id, ref)
      REFERENCES sprint_resumes (resume_id, sprint_ref) MATCH SIMPLE
      DEFERRABLE INITIALLY DEFERRED;
```

- `tasks UNIQUE (task_ref, sprint_ref)` and `sprint_resumes UNIQUE (resume_id, sprint_ref)` exist
  only as targets of these keys.
- `MATCH SIMPLE`: a composite key with a NULL column is not checked, so a sprint with no current
  task or no resume is legal.
- `DEFERRABLE INITIALLY DEFERRED`: normalized restore inserts Cards naming a Sprint before the
  Sprint row, and the Sprint then names one of them; validation happens at commit.
- Moving a card to another sprint fails while its old sprint still names it as current task; clear
  the cursor first.

### 3.4 Budget

```sql
CREATE TABLE sprint_budget_events (
    budget_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_ref      text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    event_type      text NOT NULL CHECK (event_type IN
        ('red_review','blocked','red_ci','preempt','recreated_task','hotfix',
         'infrastructure_blocked')),
    charged         boolean NOT NULL,
    task_ref        text,
    reason          text NOT NULL,
    request_id      text NOT NULL,                   -- references `requests`; see §3.9
    occurred_at     timestamptz NOT NULL,
    CONSTRAINT budget_charge_matches_type
        CHECK (charged = (event_type <> 'infrastructure_blocked'))
);
```

Deferred constraint (step 2 of §3.13):

```sql
ALTER TABLE sprint_budget_events
  ADD CONSTRAINT budget_card_is_in_this_sprint
      FOREIGN KEY (task_ref, sprint_ref)
      REFERENCES tasks (task_ref, sprint_ref) MATCH SIMPLE;
```

Budget totals (`by_type`, `total`, `signal_reached`, `hard_reached`) are aggregates over these rows
against the installation thresholds. Retry idempotency comes from the request claim (§3.9).
`request_id` is not unique here: restore may replay several exported occurrences under one restore
request. `task_ref` is nullable; a charge naming a card must name a card of that sprint.

### 3.5 Cards (tasks)

```sql
CREATE SEQUENCE card_board_key_seq;

CREATE TABLE tasks (
    task_ref       text PRIMARY KEY,                  -- "secretary-1580"
    board_key      bigint NOT NULL UNIQUE DEFAULT nextval('card_board_key_seq'),
    project_id     text REFERENCES projects(project_id),  -- NULL when the card names no project
    task_number    integer NOT NULL,
    title          text NOT NULL CHECK (title <> ''),
    description    text NOT NULL DEFAULT '',
    task_type      text CONSTRAINT task_type_is_a_known_type_or_nothing
                     CHECK (task_type IS NULL OR task_type IN ('code','research')),
    state          text NOT NULL CHECK (state IN
                     ('issues','ready','in_progress','validate','assessment','blocked','done')),
    archived       boolean NOT NULL DEFAULT false,
    position       integer NOT NULL DEFAULT 0,
    sprint_ref     text REFERENCES sprints(ref) DEFERRABLE INITIALLY DEFERRED,
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
    date_moved     timestamptz,                       -- NULL when no move time was observed
    CONSTRAINT task_board_key_is_in_card_range CHECK (board_key > 0 AND board_key < 2000000000),
    CONSTRAINT task_number_is_in_card_key_range CHECK (task_number < 2000000000),
    UNIQUE (project_id, task_number),
    UNIQUE (task_ref, sprint_ref)                     -- target of scoped keys (§3.3, §3.4, §3.8)
);

CREATE TABLE task_retry_heads (                       -- order of retry_heads
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
    task_ref        text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    depends_on      text NOT NULL,                    -- the reference as written
    depends_on_task text REFERENCES tasks(task_ref),  -- set when the store holds that card
    PRIMARY KEY (task_ref, depends_on),
    CONSTRAINT no_self_dependency CHECK (task_ref <> depends_on),
    CONSTRAINT dependency_resolution_is_the_same_reference
        CHECK (depends_on_task IS NULL OR depends_on_task = depends_on)
);

CREATE TABLE task_supersessions (                     -- supersedes
    task_ref     text PRIMARY KEY REFERENCES tasks(task_ref) ON DELETE CASCADE,
    supersedes   text NOT NULL REFERENCES tasks(task_ref),
    recorded_at  timestamptz NOT NULL,
    CONSTRAINT no_self_supersession CHECK (task_ref <> supersedes)
);
```

- `board_key` is the card's immutable protocol address; `task_number` is the public per-project
  number parsed from the reference. Neither replaces `task_ref` as identity.
- `task_type` and `project_id` are NULL when the card carries no value; the vocabulary stays
  closed.
- `depends_on_task IS NULL` means the dependency names a card the store does not hold.
- `task_supersessions` keeps one supersession per card.
- `date_moved` is set on SQL create and on every column move; Done retention reads it. Rows
  without an observed move keep NULL and are skipped by retention until they move.

### 3.6 Reservations

```sql
CREATE TABLE sprint_projects (
    sprint_ref  text    NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    project_id  text    NOT NULL REFERENCES projects(project_id),
    reserved    boolean NOT NULL DEFAULT true,
    reserved_at timestamptz NOT NULL,
    released_at timestamptz,
    ordinal     integer NOT NULL DEFAULT 0,
    PRIMARY KEY (sprint_ref, project_id),
    CONSTRAINT reserved_matches_release CHECK (reserved = (released_at IS NULL))
);

CREATE UNIQUE INDEX sprint_projects_one_live_reservation
    ON sprint_projects (project_id) WHERE reserved;
```

See §4.

### 3.7 Comments

```sql
CREATE TABLE sprint_comments (
    comment_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_ref    text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    marker        text,                               -- "po", "sprint:resume", NULL for unmarked
    body          text NOT NULL,
    actor_role    text,
    actor_id      text,
    request_id    text UNIQUE,                        -- claim key added in §3.13 step 2
    created_at    timestamptz NOT NULL
);

CREATE TABLE task_comments (
    comment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_ref    text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    marker      text,                                 -- role, or report:/review:/decision:*
    body        text NOT NULL,
    actor_role  text,
    actor_id    text,
    request_id  text UNIQUE,                          -- claim key added in §3.13 step 2
    created_at  timestamptz NOT NULL
);
CREATE INDEX task_comments_by_task ON task_comments (task_ref, created_at);

CREATE TABLE issue_comments (
    comment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    issue_id    text NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    marker      text,                                 -- a role, or issue:* (§8.1)
    body        text NOT NULL,
    actor_role  text,
    actor_id    text,
    request_id  text UNIQUE,                          -- claim key added in §3.13 step 2
    created_at  timestamptz NOT NULL
);
CREATE INDEX issue_comments_by_issue ON issue_comments (issue_id, created_at);

CREATE TABLE product_comments (
    comment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id  text NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    marker      text,
    body        text NOT NULL,
    actor_role  text,
    actor_id    text,
    request_id  text UNIQUE,                          -- claim key added in §3.13 step 2
    created_at  timestamptz NOT NULL,
    product_ref text GENERATED ALWAYS AS ('product:' || product_id) STORED
);
CREATE INDEX product_comments_by_product ON product_comments (product_id, created_at);
```

`marker` is a column; `body` is the prose without the `[marker]` prefix line. The importer strips
the prefix once (§8.1).

### 3.8 Sprint decisions

```sql
CREATE TABLE sprint_decisions (
    decision_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_ref    text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
    subject_kind  text NOT NULL CHECK (subject_kind IN ('issue','card')),
    issue_id      text,                               -- scoped below
    task_ref      text,                               -- scoped below
    verdict       text NOT NULL,
    actual        text,
    reason        text NOT NULL CHECK (reason <> ''),
    request_id    text NOT NULL,                      -- not unique: one close, many decisions
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
    ON sprint_decisions (sprint_ref, issue_id) WHERE issue_id IS NOT NULL;
CREATE UNIQUE INDEX sprint_decisions_one_per_card
    ON sprint_decisions (sprint_ref, task_ref) WHERE task_ref IS NOT NULL;
```

Deferred constraints (step 2 of §3.13):

```sql
ALTER TABLE sprint_decisions
  ADD CONSTRAINT decided_issue_is_declared_by_this_sprint
      FOREIGN KEY (sprint_ref, issue_id)
      REFERENCES sprint_issues (sprint_ref, issue_id) MATCH SIMPLE,
  ADD CONSTRAINT decided_card_is_in_this_sprint
      FOREIGN KEY (task_ref, sprint_ref)
      REFERENCES tasks (task_ref, sprint_ref) MATCH SIMPLE;
```

An issue decision is representable only for an issue the sprint declared, a card decision only for
a card the sprint holds. With `MATCH SIMPLE` each row is checked by the one key whose columns are
non-NULL.

### 3.9 Request ownership, events and idempotency

One request-id namespace for the whole installation:

```sql
CREATE TABLE requests (
    request_id  text PRIMARY KEY,
    operation   text NOT NULL,                        -- the record's kind
    intent      jsonb NOT NULL,                       -- (J5) the full audit record
    status      text NOT NULL CHECK (status IN ('staged','committed','discarded')),
    protocol    boolean NOT NULL DEFAULT false,       -- true for board.protocol_event records
    entity_kind text CHECK (entity_kind IN ('product','issue','sprint','card')),
    ref         text,
    created_at  timestamptz NOT NULL,
    settled_at  timestamptz,
    CONSTRAINT request_settled_matches_status
        CHECK ((status = 'staged') = (settled_at IS NULL)),
    CONSTRAINT requests_ref_identity UNIQUE (request_id, ref)   -- target of claim keys
);

CREATE TABLE board_events (
    event_id     text PRIMARY KEY,                    -- globally unique
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
```

Claim keys (step 2 of §3.13). Each child that carries a `request_id` references the claim together
with its own entity reference, so a child cannot hang off another entity's request:

```sql
ALTER TABLE issue_comments
  ADD COLUMN issue_ref text GENERATED ALWAYS AS ('issue:' || issue_id) STORED;
```

```sql
ALTER TABLE board_events
  ADD CONSTRAINT board_event_claims_its_request
      FOREIGN KEY (request_id, ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE task_comments
  ADD CONSTRAINT task_comment_claims_its_request
      FOREIGN KEY (request_id, task_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE issue_comments
  ADD CONSTRAINT issue_comment_claims_its_request
      FOREIGN KEY (request_id, issue_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE product_comments
  ADD CONSTRAINT product_comment_claims_its_request
      FOREIGN KEY (request_id, product_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE sprint_comments
  ADD CONSTRAINT sprint_comment_claims_its_request
      FOREIGN KEY (request_id, sprint_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE sprint_budget_events
  ADD CONSTRAINT budget_event_claims_its_request
      FOREIGN KEY (request_id, sprint_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
ALTER TABLE sprint_decisions
  ADD CONSTRAINT decision_belongs_to_its_close_request
      FOREIGN KEY (request_id, sprint_ref) REFERENCES requests (request_id, ref) MATCH SIMPLE;
```

The `UNIQUE (request_id)` on comment tables means at most one comment per claimed request.

**Request lifecycle in `SqlTaskAudit`.**

- The whole record is frozen in `requests.intent`; `operation` is its `kind`. Only records whose
  kind is an `EventKind` also get a `board_events` row. Generic records (`moved`, `edited`,
  `commented`, …) live in `requests` alone.
- `claim`/`stage` insert a `staged` row (`settled_at` NULL). `append` writes the row as
  `committed` with `settled_at` set, replacing its own staged row, and inserts the typed event.
- A mutation claims, applies and commits inside one database transaction (§7.1), so a failure
  leaves no row at all.
- A conflicting claim compares the stored record with the new one: different → `"request id
  belongs to another operation or payload"`; same and committed → replay, nothing written; same
  and staged → the existing claim is returned.
- A generic stage may replace a generic staged record, never a protocol one (`requests.protocol`).
- `discard` deletes a staged non-protocol row. `SqlTaskAudit` never writes `discarded`.
- A pending-request count is `SELECT count(*) FROM requests WHERE status = 'staged'`; it is the
  export gate (§6.3) and `secretary task verify-audit`'s answer.
- One advisory lock (`secretary.board.requests`) serializes separate claims outside a transaction;
  the per-card marker lock is also an advisory lock.

### 3.10 The seven `jsonb` columns

| Column | Content |
|---|---|
| (J1) `sprints.observer` | tagged observer union (`{"kind":"head","profile":…}`); variants belong to the head registry |
| (J2) `sprints.source_audit` | provenance of a restored or imported row (`created_at`, `updated_at`, `board`, original spelling) |
| (J3) `tasks.extensions` | unknown card metadata under `kanboard`, plus importer markers such as `board_never_named` (§8.2, §8.6) |
| (J4) `board_events.data` | per-`EventKind` payload, validated by `board/models.py` |
| (J5) `requests.intent` | the frozen audit record compared on retry |
| (J6) `issues.extensions` | (J3) for Issues |
| (J7) `products.extensions` | (J3) for Products |

No extension bag may hold a field the schema names. Link sets, budget counters, resume fields and
close decisions are relational.

### 3.11 Not in this schema

Agent runtime and head processes; the head registry (`heads/heads.yaml`, `heads/source.yaml`);
memory facts and the vector index; personas, adapters, policies; secrets; provider sessions,
quotas and credentials; Orca bindings and automations; run journals (`state/runs/**`); transcripts
and artifacts; knowledge documents.

### 3.12 Closed vocabularies

A closed vocabulary is a `CHECK` constraint on the column, never a reference table. Widening one is
an Alembic revision shipped with the code that emits the new value.

| Column | Values | Source |
|---|---|---|
| `products.state` | `active`, `archived` | `ProductState` |
| `issues.state` | `open`, `closed` | `IssueState` |
| `issues.issue_kind` | `bug`, `feature`, `question`, `improvement` | `product_issues.ISSUE_KINDS` |
| `issues.priority` | `P0`–`P3` | `product_issues.ISSUE_PRIORITIES` |
| `issues.close_reason` | `resolved`, `invalid`, `duplicate`, `wont_do` | `product_issues.ISSUE_CLOSE_REASONS` |
| `sprints.status` | `open`, `closed`, `stopped` | `SprintState` |
| `tasks.state` | the seven card states | `CardState` |
| `tasks.task_type` | `code`, `research`, or NULL | `board.task_routing.TaskType` |
| `tasks.complexity` | `cheap`, `standard`, `hard`, `frontier` | `board.task_routing.TaskComplexity` |
| `tasks.family_preference` | `auto`, `claude`, `codex` | `board.task_routing.FamilyPreference` |
| `tasks.codex_launch_mode` | `tui` | `tasks._CODEX_LAUNCH_MODES` ← `head/command.py:CODEX_LAUNCH_MODES` |
| `sprint_budget_events.event_type` | six charged types + `infrastructure_blocked` | `sprints.BUDGET_RECORDED_EVENT_TYPES` |
| `sprint_decisions.verdict` | per subject kind | `sprint_close.ISSUE_VERDICTS`, `CARD_DISPOSITIONS` |
| `requests.status` | `staged`, `committed`, `discarded` | this schema |
| `board_events.kind` | the 23 `EventKind` values (§3.9) | `board/models.py:EventKind` |
| `board_events.entity_kind`, `requests.entity_kind` | `product`, `issue`, `sprint`, `card` | `EntityKind` |
| `repositories.role` | `primary`, `curator_root` | this schema (§8.3) |

Import rules for these columns:

- a retired `codex_launch_mode` is stored as NULL (as `tasks.py` reads it); the raw value stays in
  `tasks.extensions`;
- a journal record whose kind is not an `EventKind` stays a generic `requests` row; a declared
  protocol event with an unknown kind is refused (§8.8);
- any other value outside a vocabulary is refused and named in the import report.

Open vocabularies (`claim_worker`, `slug`, `base_branch`, pins, comment markers, head/profile
columns) are not constrained; their values come from the head registry or the operator.

### 3.13 Executable order and revisions

Alembic builds the schema in two steps:

1. **Create tables** in §3 order: §3.1, §3.2, §3.3 (with `sprint_number_seq`), §3.4, §3.5 (with
   `card_board_key_seq`), §3.6, §3.7, §3.8, §3.9, plus Alembic's `alembic_version`.
2. **Add deferred constraints** in the same order: §3.3's scoped cursors, §3.4's
   `budget_card_is_in_this_sprint`, §3.8's scoped decision subjects, §3.9's generated `issue_ref`
   and the claim keys.

In `board/schema.py` every step-2 constraint carries `use_alter=True`.

Revisions (`src/secretary/board/migrations/versions/`):

| Revision | Change |
|---|---|
| `0001_initial` | tables, constraints, roles, grants and default privileges (§5.5) |
| `0002_board_gaps` | `issue_comments`; `issues.extensions`; `sprints.ref` as primary key; nullable `tasks.project_id`; `depends_on`/`depends_on_task` split |
| `0003_task_type_optional` | nullable `tasks.task_type` with `task_type_is_a_known_type_or_nothing` |
| `0004_product_issue_sql` | `product_comments`; Product/Issue `board_key`; `products.extensions`; `tasks.date_moved` |
| `0005_sprint_sql` | Sprint runtime: relation ordinals, `sprint_resumes.recorded_at_source`, deferrable card→sprint reference |
| `0006_sprint_transport_key` | `sprints.board_key` |
| `0007_card_transport_key` | `tasks.board_key` from `card_board_key_seq` |
| `0008_po_sessions` | PO head `po_sessions`, `po_turns` (one running turn per session), `po_feed` |
| `0009_po_requests` | `po_requests`: each /po form request id, its operation and input fingerprint, and the session or turn it made |
| `0010_po_session_close` | `po_sessions.closed_at`, `closed_by`, set exactly when `state = 'closed'` (`po_session_closed_iff_audited`) (head) |

`0007` upgrades an occupied `0006` store in place: it assigns keys in stable reference order,
advances the sequence past the backfill, runs `SET CONSTRAINTS ALL IMMEDIATE`, then makes the column
non-null, unique and range-checked. Refs, numbers, relations, comments and audit rows are untouched.

`0008` and `0009` only add tables, and `0010` two nullable columns and one `CHECK` that every existing
(open) row satisfies; their use is in [Operations](OPERATIONS.md#po-head-sessions-and-turns).

Catalogue at head, counted from a real `postgres:16` by `tests/test_board_store_schema.py`
(including `alembic_version`): 28 tables, 48 `CHECK`, 44 foreign keys, 28 primary keys, 17 `UNIQUE`,
5 partial unique indexes.

---

## 4. Reservations

`sprint_projects` holds reservation history; there is no separate reservations table. At most one
row per project may have `reserved = true`:

```sql
CREATE UNIQUE INDEX sprint_projects_one_live_reservation
    ON sprint_projects (project_id) WHERE reserved;
```

A second sprint reserving a held project fails with a unique violation inside its transaction.
Release is an `UPDATE`, never a `DELETE`:

```sql
UPDATE sprint_projects
   SET reserved = false, released_at = now()
 WHERE sprint_ref = 'sprint:1432' AND reserved;
```

`reserved_matches_release` forbids a released row still marked reserved.

On PostgreSQL every Sprint protocol operation runs in one transaction under
`pg_advisory_xact_lock(1600)`, shared by admission, request claims and reservations. The other
admission rules (`open_sprint_limit`, repository-tree overlap) are enforced in `sprints.py` inside
that transaction.

---

## 5. Operational boundary of the PostgreSQL installation

Operator steps are in [OPERATIONS.md](OPERATIONS.md#postgresql-board-store).

### 5.1 Container

`postgres:16` in its own Docker container, defined by `/opt/secretary/postgres-compose.yml`, which
`board/provision.py` writes and verifies (mode 0600 or narrower). Bootstrap creates it; upgrade
reconciles it (`step_board_store_provision`) before migrations. Only the database is containerized;
CLI, dispatcher, web and heads run on the host. Client tools (`psql`, `pg_dump`, `pg_restore`) are
used from inside the container.

### 5.2 Persistent volume

```yaml
services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    ports:
      - 127.0.0.1:${SECRETARY_DB_PORT}:5432
    environment:
      POSTGRES_DB: ${SECRETARY_DB_NAME}
      POSTGRES_USER: ${SECRETARY_DB_OWNER_USER}
      POSTGRES_PASSWORD: ${SECRETARY_DB_OWNER_PASSWORD}
    volumes:
      - board-db:/var/lib/postgresql/data
volumes:
  board-db:
```

- Compose project `secretary-board-store`; volume `secretary-board-store_board-db`, outside
  `<data>`, so file-level backups never copy live database files.
- Reconciliation verifies image, restart policy, loopback publication and mount before
  `compose up`; drift is refused, not repaired by recreating the container.
- An existing volume without `board-store.env` is refused: the image environment initializes only
  an empty volume.

### 5.3 Port publication

Loopback only: `127.0.0.1:<SECRETARY_DB_PORT>` (fresh default 5432) → container 5432. Clients run
on the host, so the port must be published.

### 5.4 `board-store.env`

`<instance>/board-store.env`: `KEY=VALUE`, mode 0600, git-ignored, never in the secret store and
never in archives.

```
SECRETARY_DB_HOST=127.0.0.1
SECRETARY_DB_PORT=5432
SECRETARY_DB_NAME=secretary
SECRETARY_DB_OWNER_USER=secretary_owner
SECRETARY_DB_OWNER_PASSWORD=<generated>
SECRETARY_DB_APP_USER=secretary_app
SECRETARY_DB_APP_PASSWORD=<generated>
SECRETARY_DB_READ_USER=secretary_read
SECRETARY_DB_READ_PASSWORD=<generated>
```

- All nine keys are required. An unknown, missing or empty key, a symlink, or `mode & 0o077`
  refuses the file.
- `materialize_fresh` generates three independent `secrets.token_urlsafe(32)` passwords, writes
  the git ignore first and publishes the complete file atomically. It refuses to replace an
  existing file; there is no implicit rotation.
- `store.resolve` / `resolve_role(instance, role)` is the only way to a configured store and
  enforces the git exclusion; a tracked file refuses.
- Role per connection: `board_client` defaults to `app`; `owner` is used by migration,
  provisioning and cutover/recovery database administration; `read` by read-only
  restore/cutover/recovery verification.
- Upgrade with no file: board-store steps are skipped. With a broken file: upgrade fails before
  container or migration work.

### 5.5 Roles and privileges

| Role | Privileges |
|---|---|
| `secretary_owner` | initialization superuser from `POSTGRES_USER`; owns the schema; runs migrations |
| `secretary_app` | `SELECT, INSERT, UPDATE, DELETE` on all tables, `USAGE` on sequences; no DDL |
| `secretary_read` | `USAGE` on the schema, `SELECT` only |

`0001_initial`, run as owner, creates the two login roles with passwords passed as run parameters
(`config.attributes`, rendered as literals because `CREATE ROLE` takes no bound parameter) and
grants:

```sql
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
```

Default privileges make tables added by later revisions visible to `app` and `read`.
`step_board_store_roles` verifies owner/app/read logins and the privilege boundary after
migration. The owner's `POSTGRES_PASSWORD` is read only when the volume is empty; changing it
later requires `ALTER ROLE`. Rotation procedure: [OPERATIONS.md](OPERATIONS.md#postgresql-board-store).

### 5.6 Connections

`SqlCardClient` opens one psycopg connection lazily and keeps it for its lifetime, with autocommit
off. There is no connection pool: one libpq connection carries one transaction at a time, and the
client serializes use with a re-entrant lock. The web process, dispatcher tick and each CLI process
build their own clients.

### 5.7 Backup and restore

A PostgreSQL `full` archive carries a custom-format data-only dump (`postgres_dump` component)
instead of the Kanboard `raw_board` directory; roles and credentials are not in it. Contract and
procedure: [RECOVERY.md](RECOVERY.md#backend-aware-cold-archives).

### 5.8 Python dependencies

Core dependencies in `pyproject.toml`: `psycopg[binary]>=3.2` (driver; wheels bundle libpq),
`SQLAlchemy>=2.0`, `alembic>=1.13`. `sqlalchemy`, `alembic` and `psycopg` are imported inside the
functions that need them, so an upgrade can start on a venv that lacks them. Upgrade order:
`step_dependencies` → `step_board_store_provision` → `step_board_store` (migrations) →
`step_board_store_roles` → later steps and service restarts.

---

## 6. Ownership boundaries

### 6.1 Canonical in PostgreSQL

| Data | Writer |
|---|---|
| products, issues, sprints, tasks and their link tables | `secretary_app` through the board protocol: dispatcher tick, CLI commands, `webproto/ops.py`, `webproto/sprint_ops.py` |
| `projects`, `repositories` (derived, not canonical) | importer; Product project-set and Sprint repository-list writes (§3.1) |
| comment tables | the same writers |
| `sprint_decisions`, close reason, closeout document path | `SprintWriter.close` |
| `sprint_budget_events` | `SprintWriter.record_budget`, dispatcher |
| `sprint_resumes` | `SprintWriter.resume`, observer through the CLI |
| `requests`, `board_events` | `SqlTaskAudit` (§3.9, §7.3) |

The selector (§2.2) and the normalized entity identity are process properties, not data.

### 6.2 Canonical in files and git snapshots

| Data | Location | Writer |
|---|---|---|
| project/repository bindings | `<instance>/projects/*.yaml` | operator |
| adapters, personas, policies, heads canon | `<instance>/adapters/`, `persona/`, `policies/`, `heads/` | operator; `heads/` by the heads writer |
| secrets | `<instance>/secrets/**` | secret store |
| memory facts | `<instance>/state/memory/facts/**` | memory writer |
| knowledge, incl. sprint closeouts | `<instance>/state/knowledge/**` | knowledge writer |
| run journals, claims, watermarks | `<instance>/state/runs/**` | tick writer |
| board export | `<instance>/state/board/**` | tick writer, generated from the selected backend |
| runtime config | `instance.yaml`, `runtime.env`, `board-transport.env`, `board-store.env` | operator / bootstrap / reconcile |
| transcripts, artifacts, backups, vector index | `<data>/**` | derived |

### 6.3 Exports

- The export is generated, never edited. `export_board` reads through `board_client` and
  `task_audit_for`, so on `postgres` it is generated from PostgreSQL.
- `export_board` refuses while the audit owner reports staged requests (§3.9). The Kanboard-only
  staged Product/Issue transaction journal is checked only on Kanboard.
- `state/board/events.ndjson` is a generated projection of committed audit, read by
  `board/analytics.py` from a sealed copy. Live readers never use it as the audit (§7.3).
- The checkpoint refuses to publish a truncated or rewritten export over a non-empty journal.

---

## 7. Transactions and migrations

### 7.1 One transaction per protocol mutation

`SqlCardClient.transaction()` wraps a whole protocol mutation; nested calls join it. Inside it:

1. claim the request id in `requests` (§3.9);
2. write the entity rows, links and comments;
3. insert the `board_events` row for a typed event and commit the claim.

Any failure rolls back all three. A Product/Issue create that was staged but not finished inside
the transaction aborts the commit. Sprint operations take `pg_advisory_xact_lock(1600)` first (§4).

Effects outside the database (knowledge closeout commit, head launch) happen after commit and are
recorded by their own committed events.

### 7.2 Isolation level

PostgreSQL's default `READ COMMITTED`. Invariants are held by constraints and unique indexes
(one live reservation per project, one claim per request id, one decision per subject per sprint,
one row per reference), which hold under any isolation level. Read-then-decide rules are serialized
by advisory locks: `1600` for Sprint operations, the request-namespace lock for claims, and a
per-card lock for marker comments.

### 7.3 Idempotency, audit and partial-command recovery

| Kanboard backend | PostgreSQL backend |
|---|---|
| `TaskAudit` request-id index over `events.ndjson`, plus `_product_issue_pending` | `requests.request_id` primary key; comments and budget charges claim it too |
| committed records in `events.ndjson`, pending in `pending-audit/v2-<sha256>.json`, under `.audit.lock` | `requests.status` and `board_events.committed`, under advisory locks |
| `BoardEventCanon.stage` / `commit` / `committed(request_id)` | insert/upsert on `requests`, then compare the stored `intent` |
| `_pending_owner`'s generic-stage replacement | the same rule over staged rows with `NOT protocol` |
| `TaskAudit.event_id_owner` | lookup by `intent->>'event_id'` in `requests` |
| `_require_same_event` | same comparison against `requests.intent` |
| `MutationEventTransaction` stage → effect → confirm → finish → commit | one transaction (§7.1) |
| `BoardEventPending` and `recover_*` for half-applied board writes | none: a rolled-back mutation leaves nothing; `reconcile` answers `(0, 0)` |
| `ProductIssueTransaction` staged documents | none; Product/Issue effects and claims are one transaction |
| `marker_comment_lock` (file lock per card) | advisory lock per card marker |

Caller contracts are the same on both backends: same `request_id`, same replay answer, same refusal
on reuse with another payload, same installation-wide scope.

**Card edges.** `TaskWriter._transition_card` and `TaskWriter.retire_done` run inside
`TaskWriter._mutation()`. On PostgreSQL the claim, the state/archive change, the caller's finishing
writes (claim metadata, Ready reset, reason comment) and the committed event are one transaction.
A failure is an ordinary refusal with no repair obligation. On Kanboard the effect can outlive its
record, and the caller gets `audit_pending` (exit 4) with the `recover_*` path.

**Audit owner selection.** `tasks.task_audit_for(client, data_dir)` returns
`SqlTaskAudit(client)` for a PostgreSQL client and `TaskAudit(data_dir)` for Kanboard. SQL is the
audit canon on PostgreSQL; the file journal is the canon on Kanboard. `TaskWriter`,
`SprintReader`/`SprintWriter`, `ProductIssueStore`, the dispatcher runtime and its command host take
the owner from there. Live readers:

| Reader | Reads on PostgreSQL |
|---|---|
| `CheckpointWriter` publication gate | staged `requests`; an unavailable card client blocks the checkpoint by name |
| `secretary task verify-audit` | staged count, backend named; exit 0 clean, 1 pending |
| `CommandReadLayer.command_history` / `command_request` | committed and staged `requests`; unreadable audit is `unavailable`/`unknown`, never empty or `not_found` |
| `webproto.ops` product-run publication | the `requests` row of the generic `product_run.*` record |
| `BoardEventCanon` | the audit its caller's client named; with neither audit nor data directory it refuses |
| `SprintReader` / sprint status reads | `requests` via `task_audit_for`; `_AuditOnce` has no data-directory construction |
| `ReadLayer.task_snapshot` / `task_events` | committed `requests` for the card, paged by ordinal; no file under `<data>/board` is opened |

`tests/test_architecture.py::FileAuditOwnershipTests` lists the allowed file-audit constructions:
the selector's Kanboard branch, the pre-v2 pending-layout and unmigrated-claim checks in
`product_issues.py`, and a command host built with no audit (tests only).

**Card event readers.** `ReadLayer._events` is the only selection point: it returns
`webproto.journal.EventJournal` (byte-offset reader of `board/events.ndjson`) on Kanboard or
`webproto.journal.CommittedAudit` (ordinal pages over committed audit, no file) on PostgreSQL.
The cursor names its kind, `offset` or `ordinal`; a cursor of the other kind is refused.
`EventJournal` may be constructed only there (`tests/test_architecture.py`).

### 7.4 Schema versioning and migrations

- **Schema:** `src/secretary/board/schema.py` declarative models. CHECKs are `CheckConstraint`,
  partial unique indexes are `Index(..., postgresql_where=...)`, generated refs are
  `Computed(..., persisted=True)`, deferred keys carry `use_alter=True`.
- **Revisions:** `src/secretary/board/migrations/versions/`, shipped in the package (§3.13). Each
  runs in its own transaction (`transaction_per_migration`); `0001` has no downgrade.
- **Version table:** Alembic's `alembic_version`; no other bookkeeping.
  `migrate.EXPECTED_SCHEMA_REVISION` and `migrate.head_revision()` name the head
  (`0009_po_requests`). `migrate.assert_schema_revision` is used by the importer; cutover,
  successor preparation and PostgreSQL restore compare against `head_revision()`.
- **Connection:** no `alembic.ini`. `secretary.board.migrate` builds the Alembic `Config` in code
  and passes `env.py` an owner connection from `board-store.env`; `env.py` refuses to open its own.
- **Role passwords:** read from `board-store.env`, passed in `config.attributes`, never stored in a
  revision file.
- **Lock:** `pg_advisory_lock(ADVISORY_LOCK_KEY)` on the migration session for the whole run,
  released once at the end.
- **Where it runs:** bootstrap, and `step_board_store` in `secretary upgrade` (§5.8 order).
  No file → skipped; current → unchanged; broken or tracked file, unreachable server or failed
  revision → failed, before service restart.
- **Drift check:** `tests/test_board_store_schema.py` migrates a real `postgres:16`, checks the
  §3.13 catalogue and requires an empty Alembic autogenerate diff against the models.

### 7.5 Successor database rotation

The only supported replacement for an occupied, completed import target is rotation inside the same
cluster and volume. The imported database is fenced, renamed to a bounded name derived from its
plan and database OID, and kept as evidence that ordinary roles cannot use. A new empty database is
created under the configured name and owner, migrated and role-verified. There is no row-level
merge importer. The preserved database must already be at the head revision. Command
contract and phases: [PROTOCOLS.md](PROTOCOLS.md#secretary-cutover); runbook:
[OPERATIONS.md](OPERATIONS.md#preparing-a-successor-after-a-completed-import).

---

## 8. Import mapping (Kanboard → PostgreSQL)

`board/import_board.py` (`secretary board import`) reads both Kanboard boards through the product's
readers, never writes to Kanboard, maps rows onto §3 and writes a report naming every record it
could not carry. It applies as `secretary_app` to an empty, schema-current target only; a target
that already holds board rows is refused by table name. Normalizers are imported from `tasks.py`
and `sprints.py`, not restated.

### 8.1 Metadata and marker comments → columns

| Kanboard source | Becomes |
|---|---|
| task metadata `project`, `task_type`, `slug`, `base_branch`, `seed_ref`, `complexity`, `family_preference`, `routing_reason`, `codex_launch_mode` | `tasks` columns |
| `claim` | `tasks.claim_worker`; `claimed_at` NULL (§8.6) |
| `head`, `review_head`, `resolved_head`, `resolved_review_head` | `head_override`, `review_head_override`, `resolved_worker_head`, `resolved_review_head` |
| `retry_same`, `retry_switch` | integer columns |
| `retry_heads` (delimited, `_split_heads`) | `task_retry_heads` rows in order |
| `blocked_by` | `task_dependencies` (`depends_on` always, `depends_on_task` when the card exists) |
| `supersedes` | `task_supersessions` |
| `sprint_ref` | `tasks.sprint_ref` |
| `record_type` (`task`/`issue`/`product`) | target table |
| Kanboard column | `tasks.state` via `_STATE_BY_COLUMN` |
| `is_active = 0` | `tasks.archived` |
| swimlane | derived from the product; the observed lane stays in `extensions.kanboard.swimlane` as provenance and never overrides placement |
| `sprint_goal`, `sprint_definition_of_done` | `sprints.goal`, `.definition_of_done` |
| `sprint_repositories` (paths) | `sprint_repositories` rows (§8.3) |
| `sprint_product`, `sprint_issues` | `sprints.product_id`, `sprint_issues` rows |
| `sprint_reservations` | `sprint_projects` rows (§4) |
| `sprint_status` | `sprints.status` |
| `sprint_budget`, `sprint_budget_uncharged` | `sprint_budget_events` rows (§8.7) |
| `sprint_current_task` | `sprints.current_task_ref` |
| `sprint_resume` | one `sprint_resumes` row |
| `sprint_source_audit` | `sprints.source_audit` |
| `sprint_observer`, executor pins | `sprints.observer`, `worker_pin`, `reviewer_pin` |
| `product_id`, `product_projects` | `products.product_id`, `product_projects`; other keys in `products.extensions.kanboard` |
| `issue_product`, `issue_kind`, `issue_priority`, `issue_closed_reason` | `issues` columns |
| `reference_repair` | `tasks.extensions` |
| `[marker]\nbody` comments | comment tables: `marker` + `body` |
| typed report/review/decision comments | `board_events` row from the journal **and** a comment row |
| `[secretary-product-issue-transaction:<digest>]`, `[secretary-sprint-transaction:<digest>]` witness comments | not imported as comments |

**Marker rule.** The first line is a marker only when it is a complete `[token]` line and the token
is a role in `_ROLES`, one of the prefixes `report:`, `review:`, `decision:`, `issue:`,
`validate:`, `claim:`, `watchdog:`, or one of `sprint:resume`, `archive`, `rejected`,
`steward:blocked-done`, `provision:request`. Otherwise the body is kept whole and `marker` is NULL.
`steward:blocked-done` is not the role `steward`. The rule is stricter than
`tasks._normalize_comment`, which treats any bracketed first line as a marker.

### 8.2 Metadata keys the model does not name

Keys outside `_KNOWN_METADATA` go to `extensions.kanboard` of the row's table
(`tasks`/`issues`/`products`). The report lists, per key, how many rows carry it and where it
landed; a key on many rows indicates a missing column.

### 8.3 Projects and repositories

Registry files bind a project `id` to a `repo` path (plus `remote`, `default_branch`, `adapter`,
`orca_binding`, `enabled`, optional `curator_roots`). Sprint reservations hold project ids; sprint
repositories hold paths; cards hold a project id. The importer:

1. creates one `repositories` row per distinct sprint repository path, registered or not;
2. sets `project_id` and `role` (`primary`, or `curator_root` for a curator root) where a registry
   entry matches the path, else leaves `project_id` NULL;
3. creates `projects` rows for every referenced id, with `registry_present = false` when the file
   no longer exists;
4. reports repository paths with no project, project ids with no registry file, and registry files
   no path references. None blocks the import.

Registry files stay canonical (§6.2); admission is unchanged.

### 8.4 Card→issue links

Kanboard stores no card→issue link, so `task_issues` imports empty and the report says
`task_issues: 0 rows, no source field exists`. A card reaches its issues through its sprint
(`tasks.sprint_ref` → `sprint_issues.sprint_ref`).

### 8.5 Sprint close decisions

- Closes on PostgreSQL write `sprint_decisions` in the close transaction.
- For imported closed sprints the importer materializes decisions from surviving staged transaction
  documents under `<data>/board/product-issue-transactions/`.
- Where no document survives no row is invented; the report lists the sprint under "sprints closed
  without a recoverable decision document", and the knowledge closeout remains the record.

### 8.6 Absent and empty fields

- `claimed_at`, `resolved_worker_family`, `resolved_review_family`: NULL for imported rows.
- `position`: imported as-is; afterwards an ordering integer.
- Card with no `project`: `tasks.project_id` NULL.
- Card with no `task_type`: NULL, and `tasks.extensions` records
  `{"board_never_named": ["task_type"]}`; the report lists each such card.
- `blocked_by` naming a card not on the board: `depends_on` set, `depends_on_task` NULL.
- Values the board does not carry and the importer derives (for example a closed sprint's
  `closed_at` from its modification time) are listed in the report as approximate.

### 8.7 Budget counters

Budget counters become `sprint_budget_events` rows; totals are aggregates against the installation
thresholds. A charge with a journal `budget_recorded` row links to that request. A counted event
with no surviving journal row gets a synthetic claim and an approximate `occurred_at` (sprint
`updated_at`), marked in the report. Totals reconcile exactly.

### 8.8 Audit journal and source fence

With `--data-dir`, `board/events.ndjson` is part of the source. Every nonblank row must be a JSON
object with unique nonempty `event_id` and `request_id`; it becomes one committed `requests` row
with the row frozen in `intent` and `operation` = its `kind`. Only rows declaring
`record_type=board.protocol_event` that pass `Event.from_record` are projected into
`board_events`. Budget charges reuse the journal's request claim. Any conflicting reuse of a
request id refuses the plan. Contract: [PROTOCOLS.md](PROTOCOLS.md#audit-journal-import-protocol).

The importer takes two complete observations of both boards, registry, journal and transaction
documents, recording SHA-256 identities and the journal's device, inode, size, mtime and digest.
Movement during streaming, path replacement, truncation, append or any difference between the
observations refuses before a database connection is opened. A retry reads a new pair. The fence
is a consistency check, not a write barrier.

`sprint:1037` exists twice on the Kanboard board (live and archived). The live row keeps the
reference; the archived row is stored under a distinguishing reference with its original spelling
in `sprints.source_audit`, and the report names it.

Rehearsal runbook: [OPERATIONS.md](OPERATIONS.md#rehearsing-the-complete-board-import).

---

## 9. Stable identifiers

| Identifier | In the schema |
|---|---|
| `sprint:N` and other `sprint:` references | `sprints.ref` primary key, stored verbatim; `sprints.sprint_number` nullable `UNIQUE`; new numbers from `sprint_number_seq`, set past the imported maximum |
| `<project>-<n>` task refs | `tasks.task_ref` primary key; `UNIQUE (project_id, task_number)`; the next reference is allocated over all project cards, archived included (`next_reference`) |
| `product:<id>` | `products.product_id` primary key, `products.ref` generated `UNIQUE` |
| `issue:<hash>` | `issues.issue_id` primary key, `issues.ref` generated `UNIQUE` |
| integer protocol address | `board_key` per table (§2.2) |
| sprint↔issue | `sprint_issues` |
| sprint↔project | `sprint_projects` (§4) |
| sprint↔repository | `sprint_repositories` → `repositories` |
| card→sprint | `tasks.sprint_ref` → `sprints.ref` |
| card→card dependency | `task_dependencies.depends_on`, `.depends_on_task` |
| supersession | `task_supersessions` |
| archive | `tasks.archived`; rows are never deleted |
| issue close reason | `issues.close_reason` |
| sprint close reason and account | `sprints.close_reason`, `sprints.closeout_document` (path), `sprint_decisions` |
| card decision, worker report, review verdict | `board_events` + comment `marker` |
| budgets | `sprint_budget_events` + derived totals |
| `request_id` | `requests.request_id` primary key; referenced by `board_events`, the four comment tables, `sprint_budget_events`, `sprint_decisions` (§3.9) |
| `event_id` | `board_events.event_id` primary key; `intent->>'event_id'` for generic records |
| head run reference | `board_events.head_run_ref` (reference only) |

Import keeps every reference's spelling, except the archived duplicate `sprint:1037` (§8.8).
Nothing renumbers or re-derives an existing reference.
