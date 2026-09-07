# The board store: read/write inventory and the PostgreSQL schema

Status: accepted design, no implementation. This document is the contract the remaining cards of
`sprint:1432` are cut against. It contains no SQL migration, no importer and no cutover procedure;
those are separate cards.

**The engine is given.** The owner chose PostgreSQL on 2026-09-07 (`sprint:1432`, PO comments of
09:11Z and 09:22Z). This document does not argue for or against it and contains no comparison with
SQLite. What it decides is the schema, the ownership boundary, the operational boundary of the
database installation, transactions and migrations.

**Independence from Kanboard**, as the DoD uses the phrase, means: after cutover the working cycle
does not depend on the Kanboard API or the Kanboard container as a source of truth. It is not a
prohibition on containers, and it is not an argument about how PostgreSQL is delivered.

**Not decided here.** The owner has not approved containerizing the application or the workers.
Section 5 describes the boundary of the *database* installation only. Nothing below establishes a
container runtime for the CLI, the web transport, the dispatcher or agent heads, and no later card
may cite this document as having done so.

---

## 1. Method

Every claim in section 2 comes from a search over this checkout that a reader can repeat. The
searches, and their results at the SHA this document was written against:

```bash
# (a) the protocol seam
grep -rln --include='*.py' -E '\bBoardHost\b|board_host' src

# (b) direct JSON-RPC clients
grep -rln --include='*.py' 'KanboardClient' src          # 23 files
grep -rn  --include='*.py' 'KanboardClient' src | wc -l  # 81 occurrences

# which RPC methods each module names
grep -ohE '"[a-z][A-Za-z]*(Task|Tasks|Comment|Comments|Metadata|Project|Projects|Column|Columns|Swimlane|Swimlanes|Version|Reference)[A-Za-z]*"' <file>

# the key-value domain model and the comment journal
grep -rn --include='*.py' -E 'get(TaskMetadata)|save(TaskMetadata)' src | wc -l   # 44 occurrences
grep -rn --include='*.py' -E '"(getAllComments|createComment)"' src | wc -l       # 32 occurrences

# (c) consumers of the derived exports
grep -rln --include='*.py' 'normalized_checkpoint\|validated_normalized_cards\|cards.ndjson\|export_board\|state/board' src
```

Three cautions the inventory depends on, and which any later re-run must respect:

- `grep -r` honours `.gitignore`. Run it over `src/` in a clean checkout; a "nowhere in the tree"
  conclusion from a recursive grep is not evidence about ignored paths.
- Counting `KanboardClient` counts *construction and type references*, not calls. A module that
  takes a client as a parameter (`status.py`, `restore.py`) reaches Kanboard even where it never
  builds one, and `webproto/__init__.py` matches only in prose. Both are corrected by hand below.
- A module that imports `TaskReader`/`TaskWriter`/`SprintReader`/`ProductIssueStore` reaches
  Kanboard through them even when it never names `KanboardClient`
  (`board/steward_reports.py`, `board/done_retention.py`).

---

## 2. Inventory of read/write paths

### 2.1 Group (a): through the `BoardHost` protocol seam

`src/secretary/board/host.py` defines the seam (`read`, `list`, `create`, `replace`, `transition`,
`marker_comment`, returning `MutationResult`). `src/secretary/board/kanboard.py`
(`KanboardBoardHost`) is the only production adapter; `src/secretary/board/fake.py` is its test
double. The package docstring states that the package is "additive in phase one" and that task,
sprint and Product/Issue command paths retain their current writers — which section 2.2 confirms.

| Module | Entities and fields | R/W | Own writer process? |
|---|---|---|---|
| `board/kanboard.py` | Product (`record_type`, `product_id`, `product_projects`), Issue (`issue_product`, `issue_kind`, `issue_priority`, `issue_closed_reason`), Sprint state + `SprintSupplement` (observer, `budget_by_type`), Card state, marker comments | R+W | no — a library inside its caller |
| `tasks.py` (`TaskWriter.board_host`) | Card lifecycle transitions; the event canon (`BoardEventCanon`) for `request_id` ownership | R+W | no |
| `sprints.py` (`SprintWriter._transition_host`) | Sprint lifecycle transitions only (`open`→`closed`/`stopped`/`open`) | W | no |
| `product_issues.py` | Product/Issue create, priority replace, close, via the host | R+W | no |
| `dispatcher.py` | reads `self.writer.board_host.canon` for events, attempt usage and outcome occurrences | R | yes (`secretary-dispatcher-production.service`) |

What the seam does **not** carry today: Sprint create and close bodies, Sprint metadata edits
(goal, DoD, repositories, reservations, current task, resume, budget), Card create, Card edit, Card
claim/routing metadata, Card archive, ordinary role comments, swimlane placement, board bootstrap
and every restore path. `KanboardBoardHost._migration_pending` is the explicit refusal that marks
those as unmigrated.

### 2.2 Group (b): direct `KanboardClient` / JSON-RPC

`KanboardClient` (`src/secretary/tasks.py:469`) is a generic JSON-RPC client with `call`,
`call_batch` (chunked, `_BATCH_CHUNK = 200`) and byte-size preflight. It is constructed from
`board-transport.env` (`KANBOARD_URL`, `KANBOARD_API_USER`, `KANBOARD_API_TOKEN`) via
`board_transport.resolve`.

**Core domain modules**

| Module | Entities and fields | R/W | Own writer process? |
|---|---|---|---|
| `tasks.py` — `TaskReader` | Cards: `reference`, `title`, `description`, column→`state`, `is_active`→`closed`, `position`, swimlane; metadata `project`, `task_type`, `blocked_by`, `claim`, `slug`, `base_branch`, `seed_ref`, `supersedes`, `head`, `resolved_head`, `review_head`, `resolved_review_head`, `retry_same`, `retry_switch`, `retry_heads`, `complexity`, `family_preference`, `routing_reason`, `quota_snapshot_at`, `codex_launch_mode`, `sprint_ref`, `record_type`; all comments | R | no — used by every process below |
| `tasks.py` — `TaskWriter` | Same, plus `createTask`, `updateTask`, `moveTaskPosition`, `closeTask`, `saveTaskMetadata`, `createComment`; `_READY_RESET_METADATA` clears `claim`/`resolved_head`/`resolved_review_head`/`retry_*` on a Ready transition | R+W | no |
| `tasks.py` — `TaskAudit` | Not Kanboard: the local append-only journal `<data>/board/events.ndjson`, pending records `<data>/board/pending-audit/v2-<sha256>.json`, lock `<data>/board/.audit.lock` | R+W (files) | no |
| `sprints.py` — `SprintReader`/`SprintWriter` | Sprint rows on the separate `Secretary sprints` board; metadata `sprint_goal`, `sprint_definition_of_done`, `sprint_repositories`, `sprint_product`, `sprint_issues`, `sprint_reservations`, `sprint_status`, `sprint_budget`, `sprint_budget_uncharged`, `sprint_current_task`, `sprint_resume`, `sprint_source_audit`, `sprint_observer`, executor pins; comments; `createProject` for the sprint board; `removeTask` to compensate a failed create | R+W | no |
| `product_issues.py` — `ProductIssueStore` | Product and Issue rows on the Pipeline board, distinguished by `record_type`; per-product swimlanes (`getActiveSwimlanes`, `addSwimlane`); `catalogue()` batches metadata | R+W | no |
| `board/reference_repair.py` | `updateTask` on `reference`, `saveTaskMetadata` `reference_repair` provenance | R+W | no |
| `board/steward_reports.py` | Steward report cards, via `TaskReader`/`TaskWriter` | R+W | yes when the steward tick runs (disabled in this installation) |
| `board/done_retention.py` | Archives Done cards after their dwell, via `TaskReader`/`TaskWriter` | R+W | yes when the retro tick runs (disabled) |
| `product_lanes.py` | Repairs swimlane placement: `getActiveSwimlanes`, `moveTaskPosition` | R+W | no (operator command) |

**Process entry points**

| Module | What it reaches | R/W | Own writer process? |
|---|---|---|---|
| `dispatcher.py` (`runtime_from_args`) | Builds `TaskReader`/`TaskWriter`/`TaskAudit`/`CheckpointWriter`; the tick drives every Card transition, review launch, budget charge and checkpoint | R+W | **yes** — `secretary-dispatcher-production.service`, oneshot every 60 s |
| `web/server.py` → `webproto/reads.py` | `system_snapshot`, `task_snapshot`, `task_events`: cards, sprints, comments | R | **yes** — `secretary-web.service`, `ThreadingHTTPServer`, so several concurrent readers in one process |
| `webproto/ops.py` | Card mutations from a transport | R+W | yes (same web process) |
| `webproto/sprint_reads.py` | `sprint_options` (product/issue catalogue, registered projects, heads), sprint listings | R | yes (same web process) |
| `webproto/sprint_ops.py` | `sprint_create`, sprint comment, sprint close | R+W | yes (same web process) |
| `webproto/pause_reads.py` | Pause state and what a command would reach; reads typed records | R | yes (same web process) |
| `task_commands.py` | Every `secretary task …` CLI verb | R+W | **yes** — one short-lived process per agent invocation |
| `sprint_commands.py` | Every `secretary sprint …` CLI verb | R+W | yes, same |
| `product_issue_commands.py` | Every `secretary product/issue …` CLI verb | R+W | yes, same |
| `dispatch/standing_agent.py` | Steward/retro/curator board access (`TaskReader`, `TaskWriter`) | R+W | yes — one process per scheduled tick |
| `triggered_agents/agents/curator/discover.py` | `SprintReader.show` for the curator's sprint context | R | yes — `secretary-curator.service` |
| `status.py` | Read-only installation snapshot: cards + sprints | R | yes (operator command) |
| `installation.py` | `TaskReader(...).list()` to verify a fresh install or recovery | R | yes (install/recovery command) |
| `bootstrap.py` | `getVersion`, `createProject`, `getColumns`, `addColumn`, `updateColumn`, `removeColumn`, `changeColumnPosition`, `getActiveSwimlanes`, `addSwimlane` — creates the Pipeline board itself | R+W | yes (bootstrap command, root) |
| `restore.py` | Recreates sprint rows and swimlanes from a checkpoint | R+W | yes (restore command) |
| `task_restore.py` | Recreates cards, writes `saveTaskMetadata` **verbatim** from the export, replays comments and archive state | R+W | yes (restore command) |
| `data.py` | `export_board` reads the whole board; `raw_kanboard_dump` shells `docker cp` from the container | R | yes (checkpoint/backup) |

### 2.3 Group (c): consumers of derived exports

These never speak to Kanboard. They read `<data>/board/**` or `<instance>/state/board/**`.

| Module | Consumes | Writes |
|---|---|---|
| `checkpoint.py` | regenerates the exports, validates, publishes into `state/board`, `state/runs` under `BOARD_RUNS_PATHSPEC`; seals `analytics-manifest.json` | the private instance repo |
| `board/normalized_checkpoint.py` | validates the `cards.json` / `cards.ndjson` pair and their parity | — |
| `board/analytics.py` | offline projection over a sealed `state/board` copy; joins outcome to usage by explicit event ids | — |
| `backup.py`, `backup_policy.py`, `backup_verify.py`, `backup_retention.py` | `core` archives carry `board/cards.json`, `cards.ndjson`, `export.json`; `full` additionally carries the raw Kanboard dump | tar archives under `<data>/backups` |
| `restore.py`, `restore_commands.py`, `installation.py` | read the checkpoint and hand it to the board restore | Kanboard (group b) |
| `state_repo.py` | owns the six disjoint pathspecs of the private repo | git |
| `status.py`, `webproto/reads.py`, `webproto/sprint_reads.py`, `webproto/pause_reads.py` | fall back to export freshness when a live read fails | — |
| `cli.py` | wires the above | — |

### 2.4 Consumers named by DoD 3, explicitly

- **CLI** — `task_commands.py`, `sprint_commands.py`, `product_issue_commands.py`, `cli.py`,
  `check_commands.py`, `restore_commands.py`, `host_commands.py`, `dispatcher_commands.py`.
  Group (b), one process per invocation.
- **web** — `web/server.py`, `web/app.py`, `web/pages.py`, `webfront/*` over `webproto/*`.
  Group (b), one long-lived threaded process.
- **dispatcher** — `dispatcher.py` and the `dispatcher_*.py` family, `dispatch/*`. Group (a)+(b),
  one oneshot process per 60 s tick.
- **observer** — has no module of its own. It is an agent head running the same CLI
  (`secretary sprint show/resume/comment`, `secretary task decide`) plus `sprint_observer.py` and
  `dispatcher_observer.py` on the dispatcher side. Group (b) through the CLI.
- **tasks / issues / products / sprints** — `tasks.py`, `product_issues.py`, `sprints.py`,
  `product_lanes.py`. Group (a) for the migrated edges, group (b) for everything else.
- **budget** — `SprintWriter.record_budget`, `sprint_budget` / `sprint_budget_uncharged` metadata,
  `SprintSupplement.budget_by_type` on the migrated transition. Group (a)+(b).
- **reservations** — `sprint_reservations` metadata, plus the derived guard index
  `<data>/sprints/active-repositories.json` (version 2) and the file lock
  `<data>/sprints/admission.lock`. Group (b) + local files.
- **comments** — `TaskWriter.comment`, `SprintWriter.comment`, `ProductIssueStore`, and
  `BoardHost.marker_comment`. Rendered as `[<marker>]\n<body>`; read back by
  `tasks._normalize_comment`, which recovers the marker from the first line only.
- **closeout** — `sprint_close.py` (decision file parsing, closeout document), `SprintWriter.close`,
  `knowledge_write.py`. The closeout lands in `state/knowledge/closeouts/`, not on the board.
- **audit** — `TaskAudit` (`events.ndjson`, `pending-audit/`), `BoardEventCanon`,
  `MutationEventTransaction`, `ProductIssueTransaction`
  (`<data>/board/product-issue-transactions/v1-<sha256>.json`). Files, never Kanboard.

### 2.5 Gaps, listed as gaps

1. **The card→issue link does not exist in storage.** `board/models.py` gives `Card.issue_refs`
   and `Card.product_ref`, and `board/kanboard.py:_card` populates neither. No metadata key holds
   them. `task_issues` is therefore a new relation with no source rows at cutover (§8.4).
2. **Sprint close decisions are not on the sprint.** They live in the staged transaction document
   under `<data>/board/product-issue-transactions/` and in the knowledge closeout. Only their
   effects (issue `issue_closed_reason`, archived cards) survive on the board (§8.5).
3. **`claim.claimed_at` is always `null`.** `tasks.py:_normalize` hard-codes it; only `claim` (the
   worker slug) is stored.
4. **`resolved_worker_family` / `resolved_review_family` are always `null`** for the same reason.
5. **Projects and repositories are not on the board at all.** They are `<instance>/projects/*.yaml`
   files (§8.3).
6. **Heads, profiles, adapters, policies, secrets, memory, knowledge are not on the board** and are
   out of scope for this schema by DoD 3.
7. **`board/protocol_artifacts.py`, `board/terminal_taxonomy.py`, `board/transitions.py`,
   `board/card_transitions.py`** are pure vocabulary and rule modules with no I/O. They are named
   here so a later reader does not mistake their absence from the tables for an omission.
8. **`_KNOWN_METADATA` is not exhaustive of what is stored.** Anything else on a row is preserved
   into `extensions.kanboard` by the reader, so the real key set is whatever the live board holds
   (§8.2).

---

## 3. ER schema

Scope: exactly the entities of DoD 1. Agent runtime, head registry, memory, profiles, secrets and
providers are **not** in this schema and are named here as not being in it.

Conventions: `text` for identifiers, `timestamptz` for time, surrogate `bigint` keys only where a
row has no natural key. Every reference between entities is a real foreign key. `jsonb` appears in
exactly five places, each justified inline.

The DDL below is written entity by entity for reading, not in executable order. Three references
are forward or mutual — `sprints.current_task_ref` → `tasks`, `sprints.resume_id` ↔
`sprint_resumes.sprint_number`, `sprint_budget_events.task_ref` → `tasks` — so the migration that
creates them adds those constraints with `ALTER TABLE … ADD CONSTRAINT` after both tables exist.
The mutual pair is `DEFERRABLE INITIALLY DEFERRED` for the same reason: a sprint and its first
resume are inserted in one transaction.

### 3.1 Products, projects, repositories

```sql
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
```

`projects` and `repositories` are present "to the extent of existing links" only: the registry file
stays canonical for the binding (§6), and these tables exist so that `sprint_projects.project_id`,
`tasks.project_id` and `sprint_repositories.repository_id` can be real foreign keys instead of free
text. `registry_present = false` is how a historical row survives when its `.yaml` was deleted; see
§8.3.

### 3.2 Issues

```sql
CREATE TABLE issues (
    issue_id     text PRIMARY KEY,                    -- the 20-hex suffix of issue:<id>
    ref          text GENERATED ALWAYS AS ('issue:' || issue_id) STORED UNIQUE,
    product_id   text NOT NULL REFERENCES products(product_id),
    title        text NOT NULL CHECK (title <> ''),
    description  text NOT NULL DEFAULT '',
    issue_kind   text NOT NULL,
    priority     text NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open','closed')),
    close_reason text CHECK (close_reason IN ('resolved','invalid','duplicate','wont_do')),
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    CONSTRAINT issue_close_reason_matches_state
        CHECK ((state = 'closed') = (close_reason IS NOT NULL))
);
```

The last constraint is `product_issues.py:_validate_issue_record` made relational: today an open
issue with a close reason, or a closed one without, is caught by a Python validator at export time.

### 3.3 Sprints

```sql
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
    current_task_ref   text REFERENCES tasks(task_ref) DEFERRABLE INITIALLY DEFERRED,
    resume_id          bigint REFERENCES sprint_resumes(resume_id),
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
    recorded_at          timestamptz NOT NULL
);
```

`RESUME_FIELDS` in `sprints.py` is a fixed six-field tuple, so the resume is columns, not a blob.
Resume *freshness* is derived (`_resume_freshness`) and is not stored, exactly as
`docs/RECOVERY.md` already requires of the checkpoint.

### 3.4 Budget

```sql
CREATE TABLE sprint_budget_events (
    budget_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_number   integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    event_type      text NOT NULL CHECK (event_type IN
        ('red_review','blocked','red_ci','preempt','recreated_task','hotfix',
         'infrastructure_blocked')),
    charged         boolean NOT NULL,
    task_ref        text REFERENCES tasks(task_ref),
    reason          text NOT NULL,
    request_id      text NOT NULL UNIQUE,
    occurred_at     timestamptz NOT NULL,
    CONSTRAINT budget_charge_matches_type
        CHECK (charged = (event_type <> 'infrastructure_blocked'))
);
```

Today `sprint_budget` is a counter object in one metadata value, incremented read-modify-write. As
rows, the totals in `by_type`, `total`, `signal_reached` and `hard_reached` become an aggregate
over this table against the installation thresholds — derived, so the two can no longer disagree.
`request_id UNIQUE` is what makes a retried charge idempotent without a compare-and-swap on a JSON
blob.

### 3.5 Cards (tasks)

```sql
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
    codex_launch_mode    text,
    retry_same     integer NOT NULL DEFAULT 0 CHECK (retry_same >= 0),
    retry_switch   integer NOT NULL DEFAULT 0 CHECK (retry_switch >= 0),
    extensions     jsonb NOT NULL DEFAULT '{}'::jsonb,   -- (J3)
    created_at     timestamptz NOT NULL,
    updated_at     timestamptz NOT NULL,
    UNIQUE (project_id, task_number)
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
```

`blocked_by` and `supersedes` are single-valued today. They are given their own tables rather than
columns because both are *relations between cards*: as columns they cannot carry a foreign key
without also forbidding a card that references one not yet imported, and `task_dependencies` is
where a later card adds a second dependency without a schema change. `task_supersessions` keeps its
one-row-per-card primary key so the current cardinality stays enforced.

### 3.6 Reservations

```sql
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
```

See §4.

### 3.7 Comments

```sql
CREATE TABLE sprint_comments (
    comment_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_number integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    marker        text,                               -- "po", "sprint:resume", NULL for unmarked
    body          text NOT NULL,
    actor_role    text,
    actor_id      text,
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
    request_id  text UNIQUE,
    created_at  timestamptz NOT NULL
);
CREATE INDEX task_comments_by_task ON task_comments (task_ref, created_at);
```

The marker becomes a column because today it is recovered by string-parsing the first line of the
comment body (`tasks._normalize_comment`), and a body whose first line happens to look like
`[something]` is indistinguishable from a real marker. `body` keeps the prose exactly as written,
without the `[marker]` prefix line; §8.1 says how the prefix is stripped once, at import.

### 3.8 Sprint decisions

```sql
CREATE TABLE sprint_decisions (
    decision_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_number integer NOT NULL REFERENCES sprints(sprint_number) ON DELETE CASCADE,
    subject_kind  text NOT NULL CHECK (subject_kind IN ('issue','card')),
    issue_id      text REFERENCES issues(issue_id),
    task_ref      text REFERENCES tasks(task_ref),
    verdict       text NOT NULL,
    actual        text,
    reason        text NOT NULL CHECK (reason <> ''),
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
```

Every check here is a rule `sprint_close.py` enforces in Python before a close writes anything: one
decision per ref, a verdict from the right vocabulary, a non-empty reason, and `actual` only on a
confirmation. Moving them into constraints is what stops a decision existing only in a staged
transaction file (§2.5 gap 2).

### 3.9 Events and idempotency

```sql
CREATE TABLE board_events (
    event_id     text PRIMARY KEY,
    request_id   text NOT NULL,
    kind         text NOT NULL,                       -- EventKind vocabulary
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
CREATE UNIQUE INDEX board_events_request_id ON board_events (request_id);
CREATE INDEX board_events_by_ref ON board_events (ref, occurred_at);

CREATE TABLE command_requests (
    request_id   text PRIMARY KEY,
    operation    text NOT NULL,
    intent       jsonb NOT NULL,                      -- (J5)
    status       text NOT NULL CHECK (status IN ('staged','committed','discarded')),
    result_ref   text,
    created_at   timestamptz NOT NULL,
    settled_at   timestamptz
);
```

`board_events.request_id UNIQUE` is the relational form of `BoardEventCanon`'s claim: today the
"one request id, one occurrence" rule is held by a file lock over `events.ndjson` plus an in-memory
index. `command_requests` is the relational form of `ProductIssueTransaction`'s staged intent
documents.

### 3.10 The five `jsonb` columns, and why each is one

| Column | Why it is not relational |
|---|---|
| (J1) `sprints.observer` | An observer is a tagged union today (`{"kind":"head","profile":…}`) whose variants are owned by the head registry, which DoD 3 keeps out of this schema. A relational encoding would require modelling head kinds here. |
| (J2) `sprints.source_audit` | Provenance of a *restored* row: the three fields (`created_at`, `updated_at`, `board`) a previous installation's backend reported. It describes a foreign backend, so it must not become columns that later readers mistake for this store's own audit. |
| (J3) `tasks.extensions` | The open extension point that `tasks._normalize` already produces as `extensions.kanboard`. It is where an unrecognized metadata key survives import instead of being dropped (§8.2). It is deliberately not a place for any field this schema names. |
| (J4) `board_events.data` | Per-`EventKind` payloads: marker bodies, attempt usage in five token dimensions across three accounts, outcome dispositions. Twenty-plus kinds with disjoint payloads; one table per kind is a larger change than this sprint carries, and the payloads are already validated by `board/models.py` on the way in. |
| (J5) `command_requests.intent` | The frozen argument set of an arbitrary command, compared for equality on retry. Its shape is the command's, not the schema's. |

Nothing else is JSON. In particular `product_projects`, `sprint_repositories`, `sprint_issues`,
`task_retry_heads`, `task_issues`, budget counters, resume fields and close decisions — all of
which are JSON strings in Kanboard metadata today — are relational above.

### 3.11 Not in this schema, deliberately

Agent runtime and head processes; the head registry (`heads/heads.yaml`, `heads/source.yaml`);
memory facts and the vector index; personas, adapters, policies; secrets and the secret store;
provider sessions, quotas and credentials; Orca bindings and automations; run journals
(`state/runs/**`); transcripts and artifacts; knowledge documents. DoD 3 forbids moving these in
this sprint, and none of them is a board entity.

---

## 4. Reservations

The owner's amendment: **there is no `project_reservations` table.** `sprint_projects` carries the
historical membership and a `reserved boolean`. Releasing a reservation does not delete the row.
The word `active` is not used for this; the column is `reserved`.

The constraint that forbids two live reservations of one project:

```sql
CREATE UNIQUE INDEX sprint_projects_one_live_reservation
    ON sprint_projects (project_id) WHERE reserved;
```

A partial unique index indexes only the rows matching its predicate. Rows with `reserved = false`
are not in the index at all, so a project may appear in `sprint_projects` for any number of past
sprints; at most one of those rows may have `reserved = true`. A second sprint attempting to
reserve `secretary` while `sprint:1432` holds it fails with a unique-violation on
`sprint_projects_one_live_reservation` — inside the transaction, before anything else it wanted to
write lands.

Release is an `UPDATE`, never a `DELETE`:

```sql
UPDATE sprint_projects
   SET reserved = false, released_at = now()
 WHERE sprint_number = 1432 AND reserved;
```

After it, `SELECT * FROM sprint_projects WHERE sprint_number = 1432` still returns both rows, with
`reserved = false` and a `released_at`. That is the history the DoD asks to keep, and
`reserved_matches_release` makes "released but still marked reserved" unrepresentable.

**What this replaces.** Today the same rule is held by three cooperating things outside the store:
`sprints._refuse_shared_reservations` compares candidate reservations against the other open
sprints it managed to read; `<data>/sprints/active-repositories.json` (version 2, project-keyed) is
a derived index of who holds what; and `<data>/sprints/admission.lock` serializes admission across
processes. All three exist because Kanboard has no unique constraint. The index above makes the
guard index a cache that can be rebuilt or dropped, and makes the admission lock unnecessary for
this particular invariant. `require_active_sprint_projects` — which today refuses to admit anything
when the index file is missing or of an older version — becomes a query.

**What it does not replace.** `open_sprint_limit` (2 in this installation) and the repository-tree
overlap rule in `_refuse_shared_resources` are separate admission rules, not reservations. They
stay where they are; DoD 2 forbids changing admission behaviour in this slice.

---

## 5. Operational boundary of the PostgreSQL installation

### 5.1 Install method: a container beside the application

**Chosen: `postgres:16` in its own Docker container, managed by a compose file the product writes,
exactly as Kanboard is today.** Not a system package.

The reasons, from the inventory and the host:

- The host has Docker (`/usr/bin/docker`), `postgres:16` is already pulled (the codegen stack's
  `…-db-1` runs it), and `bootstrap.py` already owns this pattern: it writes
  `/opt/secretary/kanboard-compose.yml` (mode 0600) and runs `docker compose up --detach` with
  `--env-file <instance>/board-transport.env`. A second service in that shape adds no new
  installation mechanism, no new failure mode in `bootstrap.py`, and no new privileged step.
- The host has **no `psql` and no `pg_dump`** (`which psql pg_dump` finds nothing). A system package
  would install a server *and* the client tools; a container installs the server and puts the
  client tools inside it, reachable as `docker exec secretary-postgres-1 pg_dump …`. Either works,
  and the container keeps the client tools *version-matched to the server* without adding an apt
  repository pin to `bootstrap.py`. §5.7 says what this means for backup.
- The apt path would require choosing and pinning a PostgreSQL major version against whatever
  Ubuntu ships, which is a second version authority next to the image tag.

This is the boundary of the **database** installation and nothing more. The CLI, the dispatcher,
the web transport and every agent head keep running on the host in the product venv, exactly as
now. Containerizing them is a separate, unapproved decision (PO, 2026-09-07).

### 5.2 Persistent volume

A named Docker volume, as Kanboard has (`secretary_kanboard-data` →
`/var/lib/docker/volumes/secretary_kanboard-data/_data`):

```yaml
services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    ports:
      - 127.0.0.1:5432:5432
    environment:
      POSTGRES_DB: secretary
      POSTGRES_USER: secretary
      POSTGRES_PASSWORD: ${SECRETARY_DB_PASSWORD}
    volumes:
      - board-db:/var/lib/postgresql/data
volumes:
  board-db:
```

A named volume rather than a bind mount under `<data>`: `secretary-data` is the derived plane that
`data.py:init_layout` creates and `backup.py` copies wholesale, and a live PostgreSQL data
directory inside it would be copied mid-write by a file-level backup. Keeping it out of `<data>`
makes "the database is not a file the checkpoint snapshots" a property of the layout, not a rule
somebody has to remember. The database's backup path is §5.7.

### 5.3 Port publication

**Published, on loopback only: `127.0.0.1:5432:5432`.** This is forced by §5.1: the clients run on
the host, not in the compose network, so an unpublished port would be unreachable. It is the same
exposure Kanboard already has (`127.0.0.1:8080:80`), and the same one the host firewall already
accounts for. The codegen stack's production compose does not publish its DB port because its
application container shares the network; that reasoning does not transfer to an application on the
host, and citing it would be citing an unapproved containerization of this application.

### 5.4 Connection string and where it lives

**A new `board-store.env` beside `board-transport.env` in the private instance directory, mode
0600, git-ignored, with the same lifecycle.**

`board_transport.py` already establishes this exact category: "non-secret, local … configuration",
a `KEY=VALUE` file at `<instance>/board-transport.env`, parsed by
`triggered_agents/runtime/board_transport.py:parse`, which refuses a symlink, refuses
`mode & 0o077`, refuses unknown keys and refuses a partial tuple. It has a deterministic
fresh-install default, is reconciled rather than silently repaired, and is passed to
`docker compose --env-file` so the server and its clients cannot disagree about the credential. A
board-store connection is the same kind of thing, and reusing the mechanism means the DSN gets the
mode check, the reconciliation and the ignore-lifecycle for free.

```
SECRETARY_DB_HOST=127.0.0.1
SECRETARY_DB_PORT=5432
SECRETARY_DB_NAME=secretary
SECRETARY_DB_USER=secretary
SECRETARY_DB_PASSWORD=<generated at bootstrap>
```

**Why not the secret store.** `secret_store.py` exists for values whose loss makes an installation
unrecoverable and which must survive a rebuild on a clean host from a recovery phrase. The database
password is not one: it is regenerable by recreating the role, it is meaningless without the volume
it guards, and it is needed by `docker compose up` *before* the instance repository is necessarily
in a state where the store can be opened. Kanboard's API token was excluded from the store for
exactly this reason and this follows it. What *does* belong in the secret store, if and when it
exists, is an off-host replication or backup credential — the same distinction
`github.checkpoint-token` already draws.

**How each consumer gets it.** Unchanged in shape from `KanboardClient.for_instance`: one resolver,
`board_store.resolve(instance_dir)`, reading the file the same way `board_transport.resolve` does.

- CLI (`task_commands.py`, `sprint_commands.py`, `product_issue_commands.py`): from
  `--instance` / `SECRETARY_INSTANCE`, per invocation.
- web (`secretary-web.service`): from `SECRETARY_INSTANCE`, already in the unit.
- dispatcher (`secretary-dispatcher-production.service`): from `SECRETARY_INSTANCE`, already in the
  unit, plus its `EnvironmentFile=-<instance>/runtime.env`.
- observer, worker, reviewer heads: they run the CLI, and `role_env`/`runtime_env` already
  propagate `SECRETARY_INSTANCE` and `SECRETARY_DATA_DIR` into every head process. No new
  propagation.

### 5.5 Roles and privileges

Three roles, so that a read-only consumer cannot write and the migration runner is not the
application:

| Role | Used by | Privileges |
|---|---|---|
| `secretary_owner` | migrations only (§7.4) | owns the schema; `CREATE`, `ALTER`, `DROP` |
| `secretary_app` | dispatcher, CLI writers, `webproto/ops.py`, `webproto/sprint_ops.py` | `SELECT, INSERT, UPDATE, DELETE` on all tables, `USAGE` on sequences; **no** DDL |
| `secretary_read` | `status.py`, `webproto/reads.py`, `webproto/sprint_reads.py`, `webproto/pause_reads.py`, `data.py:export_board`, `board/analytics.py` | `SELECT` only |

`DELETE` is granted to `secretary_app` because the current writers genuinely delete: `removeTask`
compensates a failed sprint create, and `ON DELETE CASCADE` on the link tables is how a compensated
create leaves nothing behind. Nothing in the current product deletes a card or a closed sprint —
archival is `archived = true`, and §4 releases a reservation by `UPDATE`.

The `secretary` bootstrap role from the compose environment is `secretary_owner`; the other two are
created by the first migration.

### 5.6 Pool and the number of real concurrent writers

From §2, on this installation as configured (`instance.yaml`: `curator`, `dispatcher-production`,
`memory` enabled; `steward`, `retro`, `steward-deep-sweep` disabled; `open_sprint_limit: 2`):

| Writer | Kind | Concurrency |
|---|---|---|
| `secretary-dispatcher-production.service` | oneshot, every 60 s | 1 process, sequential within a tick |
| `secretary-web.service` | long-lived `ThreadingHTTPServer` | 1 process, N threads; writes only from `ops.py` / `sprint_ops.py` |
| `secretary-curator.service` | oneshot, hourly | 1 process; reads a sprint |
| agent-head CLI (`secretary task report/comment/…`) | short-lived | ≤ 1 observer + ≤ 2 sprints × (worker + reviewer) ≈ ≤ 5 at a peak |
| operator CLI, `secretary status` | short-lived | 1–2 |
| `checkpoint`/`backup` | inside the dispatcher tick, or an operator command | 1 |

So the ceiling is roughly **ten concurrent connections**, and the steady state is two or three. The
memory MCP does not touch the board at all.

Sizing that follows: `max_connections = 50` (the `postgres:16` default of 100 is more than needed
and costs memory); per-process pooling of **1–2 connections for a short-lived CLI process** (a
process that runs one command needs one connection) and a small pool (**max 8**) in the web
process, which is the only place several threads want a connection at once. No PgBouncer: at this
connection count it is a component to operate for no measured benefit, and the measurement that
would justify it does not exist yet.

Board size for sizing: 1482 Pipeline cards, open and archived, on 2026-09-06
(`state/knowledge/measurements/2026-09-06-sprint-form-catalogue-n-plus-one.md`). Every table in §3
is small; the /sprints/new budget the same measurement fixed (≤ 3 s, and 8 batched metadata reads
instead of 3000) becomes one indexed join.

### 5.7 Backup and restore of PostgreSQL, against the existing chain

The existing chain, unchanged in its own terms:

1. `checkpoint.py` regenerates the normalized exports, validates them
   (`board/normalized_checkpoint.py`), seals `analytics-manifest.json`, and commits
   `state/board` + `state/runs` into the private instance repository under `BOARD_RUNS_PATHSPEC`.
   Once per dispatcher tick on change; pushed in a 30-minute window; durable RPO 30 minutes
   (`docs/RECOVERY.md`, "Cadence and RPO").
2. `backup.py` writes `core` and `full` tar archives; `core` carries `board/cards.json`,
   `cards.ndjson`, `export.json`; `full` additionally carries a raw Kanboard `docker cp` dump.
3. `restore.py` / `task_restore.py` rebuild the board from the normalized export.

What changes:

- **The normalized export stays, and stays the recovery canon.** It is generated from PostgreSQL
  instead of from Kanboard. `export_board` swaps its reader; the file contract
  (`cards.ndjson` / `sprints.ndjson` / `events.ndjson` / `export.json` + the seal), the validation
  and the pathspecs do not change, so `docs/RECOVERY.md`'s "What the checkpoint contains" stays
  true word for word and `board/analytics.py` keeps working on a copied `state/board` directory.
- **A PostgreSQL dump is added as a second, derived artefact — not a second canon.** One
  `pg_dump --format=custom` per backup run, taken through the container
  (`docker exec secretary-postgres-1 pg_dump -U secretary -Fc secretary`) because §5.1 put the
  client tools there, written to `<data>/backups/…/board-db.dump`, and carried by the **`full`**
  policy only. `core` stays exactly what it is today: the normalized, portable, engine-independent
  set. A `full` archive already carries the engine-specific raw board dump for Kanboard; the
  PostgreSQL dump takes that slot after cutover.
- **`full`'s `raw_board` component changes source, not meaning.** `data.py:raw_kanboard_dump`
  (`docker cp` of `/var/www/app/data`) is replaced by the `pg_dump`; `backup_policy.py`'s
  `requires_raw_board_data` flag and the `full` retention (48 h) apply unchanged.
- **Consistency.** `pg_dump` takes a single consistent snapshot by itself; no writer pause is
  needed for the backup, which is strictly better than the raw Kanboard `docker cp` it replaces.
  `backup.py`'s existing pipeline pause is about the rest of the archive, not the database, and is
  not weakened.
- **Restore order.** Fast path: `pg_restore` the custom dump from a `full` archive. Portable path,
  and the one `docs/RECOVERY.md` promises: migrate an empty database to the current schema version,
  then import the normalized checkpoint through the same importer the cutover uses. The second path
  is what makes a recovery on a clean host independent of the dump format and of the exact server
  version.
- **The `restore_capability` values (`normalized-core`, `full-snapshot`) keep their meanings.** A
  `core` archive still restores the normalized board and nothing engine-specific.

`docs/RECOVERY.md`'s "Source of truth" section — "the live board backend stays the operational
store, the remote Git HEAD is the last confirmed recovery checkpoint" — is unchanged: only the
identity of the live backend changes.

### 5.8 The Python driver as a new core dependency

**`psycopg[binary] >= 3.2`**, added to `[project].dependencies` in `pyproject.toml`.

- **Which package.** `psycopg` 3 is the current driver; `psycopg2` is maintenance-only. The
  `binary` extra ships manylinux wheels that bundle libpq, so the install needs **no** `libpq-dev`,
  no compiler and no apt step — which matters precisely because §5.1 declined to install the
  PostgreSQL apt packages on the host.
- **Core, not optional.** After cutover an installation that cannot reach its board is not
  degraded, it is non-functional. The same argument `pyproject.toml` already records for
  `cryptography`: "an installation that cannot open its own secrets is not recoverable." A
  `[project.optional-dependencies]` entry would let `pip install -e .` produce an installation that
  imports and then fails on first use.
- **How it is installed.** By the existing path and no new one: `upgrade.py:step_dependencies`
  detects that a dependency manifest moved and runs
  `python -m pip install --quiet -e '<product_root>[dev]'` into `<product_root>/.venv`. Adding a
  dependency to `pyproject.toml` is exactly the trigger that step watches for, so a normal
  `secretary upgrade` installs it. `bootstrap.py` gets it on a fresh install through the same venv
  install.
- **Offline installation.** This is a real regression in the offline story and is named rather than
  papered over. Today the three core dependencies (PyYAML, jsonschema, cryptography) are already
  PyPI wheels, so `pip install -e .` on a host with no index already fails; a fourth wheel does not
  change the *kind* of failure. What it does change is that the failure now happens on a host that
  previously might have had the three cached. Two consequences the delivery card must handle:
  (1) `psycopg[binary]` must be present in the venv **before** the first migration runs, so the
  upgrade step ordering is dependencies → migrations → services; (2) an air-gapped install needs
  the wheel staged the same way the other three are, and `secretary upgrade` must report
  `dependencies: failed` loudly rather than proceeding to a migration it cannot run. No new
  offline mechanism is introduced by this card.
- **Version pin.** A floor (`>= 3.2`), not an equality pin, matching how PyYAML, jsonschema and
  cryptography are declared. Ruff is the only equality-pinned dependency, because a linter's
  version is part of a gate's meaning; a driver's is not.

---

## 6. Ownership boundaries

After cutover, for each thing: what is canonical, and who writes it.

### 6.1 Canonical in PostgreSQL

| Data | Writer after cutover |
|---|---|
| products, issues, sprints, tasks | `secretary_app`, through the board protocol: dispatcher tick, CLI commands, `webproto/ops.py` and `webproto/sprint_ops.py` |
| `product_projects`, `sprint_projects`, `sprint_repositories`, `sprint_issues`, `task_issues` | same |
| `task_dependencies`, `task_supersessions` | same |
| `sprint_comments`, `task_comments` | same |
| `sprint_decisions`, sprint close reason and closeout document *path* | `SprintWriter.close` |
| `sprint_budget_events` | `SprintWriter.record_budget`, dispatcher |
| `sprint_resumes` | `SprintWriter.resume`, observer through the CLI |
| `board_events`, `command_requests` | the protocol seam (`BoardEventCanon`, `MutationEventTransaction`) |
| card state, archive, claim, routing | dispatcher and CLI writers |

### 6.2 Canonical in files and git snapshots

| Data | Location | Writer after cutover | Changed? |
|---|---|---|---|
| project/repository bindings | `<instance>/projects/*.yaml` | the operator, by hand | no |
| adapters, personas, policies, heads canon | `<instance>/adapters/`, `persona/`, `policies/`, `heads/` | operator; `heads/` by the heads writer (`HEADS_PATHSPEC`) | no |
| secrets | `<instance>/secrets/**` | the secret store (`SECRETS_PATHSPEC`) | no |
| memory facts | `<instance>/state/memory/facts/**` | the memory writer (`MEMORY_PATHSPEC`) | no |
| knowledge, including sprint closeouts | `<instance>/state/knowledge/**` | the knowledge writer (`KNOWLEDGE_PATHSPEC`) | no |
| run journals, claims, watermarks | `<instance>/state/runs/**` | the tick writer (`BOARD_RUNS_PATHSPEC`) | no |
| board **export** | `<instance>/state/board/**` | the tick writer (`BOARD_RUNS_PATHSPEC`) | source changes, writer does not |
| runtime config | `<instance>/instance.yaml`, `runtime.env`, `board-transport.env`, new `board-store.env` | operator / bootstrap / reconcile | one new file |
| transcripts, artifacts, backups, vector index | `<data>/**` | derived, rebuilt | no |

The six disjoint pathspecs of `state_repo.py` are unchanged in number, name and ownership. The
board-store cutover adds no seventh writer to the private repository.

### 6.3 Why the exports do not become a second writer

Three properties, each already true and each preserved:

1. **The export is generated, never edited.** `CheckpointWriter._regenerate` rebuilds
   `state/board` from the live board on every tick; nothing reads a card back out of
   `cards.ndjson` and writes it to the board except `restore.py`, which is a recovery command run
   by an operator against a deliberately empty board.
2. **The direction is one-way and gated.** `export_board` refuses to run while any pending audit
   record or Product/Issue transaction is unresolved, so an export can never publish a half-applied
   mutation as a fact. That gate becomes a query over `board_events WHERE NOT committed` and
   `command_requests WHERE status = 'staged'` — the same refusal, cheaper.
3. **The checkpoint refuses to lose history.** `_prevent_run_history_loss` refuses to publish a
   truncated or rewritten live export over a non-empty canonical journal. Unchanged.

What must not happen, stated so a later card can be checked against it: **no Kanboard export may be
published into `state/board` after cutover.** Kanboard becomes a read-only archive; a checkpoint
that regenerated from it would overwrite the PostgreSQL-derived cut with a stale one and would look
exactly like a successful tick. The cutover card owns disabling that path, not merely not using it.

---

## 7. Transactions and migrations

### 7.1 One transaction per protocol mutation

Today a mutation that touches several entities is several JSON-RPC calls with no atomicity, and
the product compensates with `MutationEventTransaction`: stage a record, apply exactly one backend
effect, confirm it with a separate read, run the remaining idempotent work, then commit the record.
`BoardEventPending` is raised for every failure after the effect, leaving a pending record for
`recover_marker_comment`, `recover_sprint`, `recover_transition` and `recover_product_issue`
(`board/kanboard.py`).

In PostgreSQL the whole of that becomes one transaction:

```sql
BEGIN;
  INSERT INTO command_requests (request_id, operation, intent, status, created_at)
       VALUES (:rid, 'sprint.create', :intent, 'committed', now())
  ON CONFLICT (request_id) DO NOTHING;        -- second insert affects 0 rows -> replay
  -- if 0 rows: verify the stored intent equals :intent, then COMMIT and return the existing result
  INSERT INTO sprints (...) VALUES (...);
  INSERT INTO sprint_projects (...) SELECT ...;   -- may raise on the partial unique index
  INSERT INTO sprint_issues (...) SELECT ...;
  INSERT INTO sprint_repositories (...) SELECT ...;
  INSERT INTO board_events (...) VALUES (..., committed => true, committed_at => now());
COMMIT;
```

A sprint create either lands whole — row, reservations, issue links, repository links and its event
— or lands not at all. A close does the same for the card archives, the issue closures, the
`sprint_decisions` rows, the reservation release and the status change. There is no window in which
a sprint is closed but still holds a reservation, which is a window the current close comments on
explicitly ("Publish closed last so unfinished closes retain project reservations").

Two effects that are genuinely outside the database stay outside it, and stay ordered after the
commit: writing the knowledge closeout through the knowledge writer, and launching a head. The rule
that a database write is not evidence that an external process started (DoD 5) is unchanged: the
launch is recorded by its own committed event *after* it happened, exactly as now.

### 7.2 Isolation level

**`READ COMMITTED`** — PostgreSQL's default — plus explicit locking where a decision depends on
rows the transaction did not write.

Why it is sufficient here:

- Every invariant this store actually needs is a *constraint*, not a read: one live reservation per
  project (partial unique index), one occurrence per `request_id` (unique index), one decision per
  ref per sprint (partial unique indexes), one reference per family (unique index, §9). Constraints
  are enforced by the index regardless of isolation level, so the classic read-check-write race
  that `admission.lock` guards today cannot occur at any level.
- The remaining read-then-decide cases are admission rules that count rows the transaction does not
  modify: `open_sprint_limit` and the repository-overlap refusal. `READ COMMITTED` permits a
  phantom there. Both take `SELECT ... FOR UPDATE` on the candidate projects' rows — which the
  reservation insert would lock anyway — so two concurrent sprint creates serialize on the same
  rows. Where a rule genuinely counts sprints rather than projects (`open_sprint_limit`), the
  transaction takes one advisory lock (`pg_advisory_xact_lock` on a fixed admission key), which is
  the direct successor of `<data>/sprints/admission.lock` and is released by the commit rather than
  by a process that might die holding a file lock.
- `SERIALIZABLE` would buy nothing beyond that and would introduce serialization failures the
  callers would have to retry. With ≤ 10 concurrent connections (§5.6), almost all of them readers,
  the contention that would justify it does not exist.

Read-only consumers (`status.py`, `webproto/reads.py`, the export) run in `READ ONLY` transactions.
`export_board` runs in one `REPEATABLE READ` read-only transaction so that cards, sprints and
events in a single checkpoint cut are one consistent snapshot — which is a property the current
multi-pass Kanboard export does not have.

### 7.3 Idempotency, audit and partial-command recovery

| Today | After |
|---|---|
| `TaskAudit` committed/pending records in `events.ndjson` + `pending-audit/v2-<sha256>.json`, indexed under a file lock | `board_events.request_id UNIQUE`; `committed` boolean |
| `BoardEventCanon.stage` / `commit` / `committed(request_id)` | `INSERT … ON CONFLICT (request_id) DO NOTHING`, then compare the stored payload |
| `_require_same_event`: "request id belongs to another operation or payload" | the same comparison against the stored `board_events` row / `command_requests.intent` |
| `MutationEventTransaction`'s stage → effect → confirm → finish → commit | one transaction; `confirm` disappears, because a committed transaction *is* the confirmation |
| `BoardEventPending` and the four `recover_*` entry points | a transaction that does not commit leaves nothing; nothing to recover |
| `ProductIssueTransaction` staged intent documents | `command_requests` rows with `status = 'staged'` |
| `marker_comment_lock` (per-card flock, because marker prose carries no request id) | `task_comments.request_id UNIQUE`; markers get a request id at the seam |

The contracts callers see are unchanged: the same `request_id`, the same "retry with the same
request id" answer, the same refusal when a request id is reused with a different payload. What
disappears is the class of failure where the effect landed and the record did not — the reason
`recover_*` exists at all.

`pending-audit/` and the staged transaction documents must be **empty at cutover**, not migrated: a
pending record describes a half-applied Kanboard write, and there is nothing in PostgreSQL for it
to be half-applied to. `export_board`'s existing refusal to export while any pending record exists
is exactly the precondition, and the cutover card should use it as its gate.

The audit journal itself — `state/board/events.ndjson` — keeps being written, generated from
`board_events` by the checkpoint. `board/analytics.py` reads a sealed copy and is untouched.

### 7.4 Schema versioning and migrations

**A minimal in-product migration runner. No new dependency.**

```sql
CREATE TABLE schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    applied_at  timestamptz NOT NULL,
    checksum    text NOT NULL
);
```

- Migrations are numbered `.sql` files shipped in the product tree
  (`src/secretary/board/migrations/0001_initial.sql`, …), applied in order, each inside its own
  transaction together with its `schema_migrations` insert. PostgreSQL has transactional DDL, so a
  failed migration leaves the version it was moving from, not a half-applied schema.
- A `checksum` mismatch on an already-applied version is a hard refusal, not a re-apply: an edited
  historical migration means the running schema and the tree disagree, and guessing which is right
  is how a store loses data.
- The runner takes `pg_advisory_lock` on a fixed migration key, so two upgrades cannot race.
- It runs as `secretary_owner`; the application role has no DDL (§5.5).
- Every process asserts the expected version at startup and refuses to write on a mismatch — the
  same shape as `board_transport`'s refusal of a partial configuration and `TaskAudit`'s
  `upgrade_required` refusal of the pre-v2 pending layout. A dispatcher writing through a schema it
  does not know is worse than a dispatcher that stops.

Why not Alembic or another framework: the product's dependency list is deliberately three packages,
and §5.8 already adds a fourth for a reason that has no alternative. A framework here would add a
migration DSL, a second configuration file and a second CLI to an installation whose whole
migration need is "apply numbered SQL files in order, once, under a lock". `upgrade.py` already
owns ordered idempotent steps with typed results (`StepResult`), and the runner is one more of
them, placed after `step_dependencies` (§5.8) and before any service restart.

---

## 8. Data compatibility

### 8.1 Metadata bags and marker comments → columns

| Today | Where | Becomes |
|---|---|---|
| `project`, `task_type`, `slug`, `base_branch`, `seed_ref`, `complexity`, `family_preference`, `routing_reason`, `codex_launch_mode` | task metadata strings | `tasks` columns, with the CHECK constraints `tasks.py` enforces in Python (`_TASK_TYPES`, `_COMPLEXITIES`, `_FAMILY_PREFERENCES`, `_CODEX_LAUNCH_MODES`) |
| `claim` | task metadata | `tasks.claim_worker`; `claimed_at` stays NULL for imported rows (§8.6) |
| `head`, `review_head`, `resolved_head`, `resolved_review_head` | task metadata | `head_override`, `review_head_override`, `resolved_worker_head`, `resolved_review_head` |
| `retry_same`, `retry_switch` | numeric strings | integer columns with `>= 0` |
| `retry_heads` | delimited string, split by `_split_heads` | `task_retry_heads` rows, order preserved in `ordinal` |
| `blocked_by` | task metadata (single ref) | `task_dependencies` row |
| `supersedes` | task metadata (single ref) | `task_supersessions` row |
| `sprint_ref` | task metadata | `tasks.sprint_number` FK |
| `record_type` (`task`/`issue`/`product`) | task metadata | table identity: the three record types become three tables |
| column name (`Ready`, `In progress`, …) | Kanboard column | `tasks.state`, via `_STATE_BY_COLUMN` |
| `is_active = 0` | Kanboard row status | `tasks.archived` |
| `swimlane` | Kanboard swimlane, per product | derived from the product; kept in `extensions` for provenance (§8.2) |
| `sprint_goal`, `sprint_definition_of_done` | sprint metadata | `sprints.goal`, `.definition_of_done` |
| `sprint_repositories` | JSON array of paths | `sprint_repositories` rows (§8.3) |
| `sprint_product`, `sprint_issues` | metadata / JSON array | `sprints.product_id`, `sprint_issues` rows |
| `sprint_reservations` | JSON array of project ids | `sprint_projects` rows with `reserved = true` (§4) |
| `sprint_status` | metadata | `sprints.status` |
| `sprint_budget`, `sprint_budget_uncharged` | JSON counter objects | `sprint_budget_events` rows; the counters become an aggregate (§8.7) |
| `sprint_current_task` | metadata | `sprints.current_task_ref` FK |
| `sprint_resume` | JSON object, six fields | one `sprint_resumes` row, columns |
| `sprint_source_audit` | JSON | `sprints.source_audit` jsonb (J2) |
| `sprint_observer`, executor pins | encoded strings | `sprints.observer` jsonb (J1), `worker_pin`, `reviewer_pin` |
| `product_id`, `product_projects` | metadata / JSON array | `products.product_id`, `product_projects` rows |
| `issue_product`, `issue_kind`, `issue_priority`, `issue_closed_reason` | metadata | `issues` columns with CHECKs |
| `reference_repair` | metadata provenance | `tasks.extensions` (it describes a Kanboard-era repair) |
| `[role]\nbody` comments | Kanboard comments | `task_comments` / `sprint_comments`: first line parsed once at import into `marker`, remainder into `body` |
| `[report:done]`, `[report:blocked]\nclassification: …`, `[review:green|red]`, `[decision:release|rework|reslice]` | Kanboard comments rendered from typed events | `board_events` rows (`data` carries `marker`, `body`, `status`, `classification`, `decision`) **and** a `task_comments` row; the event is the fact, the comment is its rendering, exactly as `EventKind`'s comment already says |
| `[secretary-product-issue-transaction:<digest>]`, `[secretary-sprint-transaction:<digest>]` | Kanboard comments used as transaction witnesses | **not** carried forward as comments: they exist because Kanboard has no transaction. They import into `command_requests` as settled rows, with their digest retained for traceability |

**Import rule for the comment prefix.** The `[marker]` line is stripped exactly once, at import,
and only when the first line is a complete `[…]` on its own line and the token matches a known
marker vocabulary (a role in `_ROLES`, or `report:*` / `review:*` / `decision:*` / `issue:*` /
`sprint:resume` / `archive` / `rejected`). A first line that merely looks like a marker but is not
in the vocabulary keeps the whole body verbatim and gets `marker = NULL`. This is stricter than
today's `_normalize_comment`, which treats *any* bracketed first line as a marker, and it is
stricter in the safe direction: it can only fail to recognize a marker, never eat a line of prose.

### 8.2 Metadata keys the model does not name

`tasks._normalize` already collects every key outside `_KNOWN_METADATA` into
`extensions["kanboard"]`. The importer does the same into `tasks.extensions` (J3). This is what
keeps "simplify the schema and lose records" from happening by accident: a key nobody remembers
writing survives the import and is visible in a query, rather than being dropped because it was not
in a hand-written column list. The dry-run report of the importer card must list, per key, how many
rows carry it and where it landed — a key that lands in `extensions` for thousands of rows is a
missing column, and the report is how that gets noticed before cutover, not after.

### 8.3 The project/repository mismatch (DoD 2)

**The facts.** Projects are not board data. They are `<instance>/projects/<id>.yaml` files —
18 of them in this installation — read by `product_issues.registered_projects`, which collects only
the `id` field. Each file binds one `id` to one `repo` path, optionally a `remote`, a
`default_branch`, optional `integration_bases`, an `adapter`, an `orca_binding`, `enabled`, and
optionally `curator_roots` (extra roots; `codegen-orchestrator` has one).

Meanwhile:

- `sprints.reservations` holds **project ids** (`["secretary", "secretary-instance"]`).
- `sprints.repositories` holds **absolute filesystem paths**
  (`["/home/dev/secretary", "/home/dev/secretary-instance"]`).
- `tasks.project` holds a **project id**.

The two sprint lists are related only by convention: `/home/dev/secretary` is the `repo` of project
`secretary` because somebody typed both. Nothing enforces it, `_refuse_shared_resources` compares
repository *paths* by tree containment rather than by project, and a sprint may name a repository
path that belongs to no registered project at all.

**The problem.** A naive `sprint_repositories(repository_id) REFERENCES repositories` rejects, at
import, exactly the historical rows whose path has no registry entry — which is a record loss, and
the DoD forbids it.

**The resolution, in order:**

1. `repositories` is keyed by `path` with a `UNIQUE (path)` and a **nullable** `project_id`. The
   importer creates one `repositories` row per distinct path found in *any* sprint's
   `sprint_repositories`, whether or not a registry entry claims it.
2. Where a registry entry's `repo` (or a `curator_roots` entry) equals that path, the row gets that
   `project_id` and `role = 'primary'` (or `'curator_root'`). Where none does, `project_id` stays
   NULL: the sprint keeps its repository, and the absence of a binding is recorded as an absence
   rather than invented.
3. `projects` rows are created for every project id referenced by any historical sprint reservation
   or card, including ids whose `.yaml` no longer exists. Those get `registry_present = false`.
   `registered_projects()` keeps reading the files; the table is what lets a foreign key exist over
   history.
4. The importer's dry-run report lists, as its own section: repository paths with no project,
   project ids with no registry file, and registry files whose `repo` no path references. All three
   are facts about the current data, and all three are for the owner to look at — none of them
   blocks the import.
5. `repositories_one_primary` allows at most one primary repository per project, which is the
   registry's actual cardinality today. It does not forbid a second row for the same project with
   `role = 'curator_root'`, which the registry already has.

What this explicitly does **not** do: it does not make the registry files derived from the
database, it does not change how a sprint names its repositories, and it does not change admission.
The registry stays canonical (§6.2); these tables exist so history has somewhere to point.

### 8.4 The card→issue link has no source data

`task_issues` (§3.5) is a real relation in the model (`Card.issue_refs`) with **no storage today**:
no metadata key holds it, and `board/kanboard.py:_card` builds every Card with an empty
`issue_refs`. Importing it therefore produces zero rows, and that is the correct outcome — not a
sign the importer missed something.

The resolution: the table is created empty, and the importer's report states "task_issues: 0 rows,
no source field exists" so that nobody later reads an empty table as data loss. A card's issue
association is reachable today through its sprint (`tasks.sprint_number` → `sprint_issues`), which
is what the current product actually uses. Populating `task_issues` per card is a product change
and belongs to a later card, not to the import.

### 8.5 Sprint close decisions have no board home

Per §2.5 gap 2, a close's decisions live in `<data>/board/product-issue-transactions/v1-*.json` and
in the knowledge closeout; the board keeps only their effects. So:

- For sprints closed **after** cutover, `sprint_decisions` is written in the close transaction
  (§7.1) and is canonical.
- For sprints closed **before**, the importer reads the staged transaction documents still present
  under `<data>/board/product-issue-transactions/` and materializes what it finds. Those files are
  retained per the transaction store's own lifecycle, not forever, so coverage will be partial.
- Where no document survives, the row is **not** invented. The importer records the sprint in its
  report under "closed without recoverable decisions", and the knowledge closeout stays the
  human-readable record — which it already is, and which `sprints.closeout_document` points at.

Losing nothing means not deleting the closeouts and not pretending a reconstruction is a record.

### 8.6 Fields that are structurally absent

- `claim.claimed_at` — hard-coded NULL in `tasks._normalize`; imports as NULL, and the column
  starts being written for claims made after cutover.
- `resolved_worker_family` / `resolved_review_family` — same.
- `position` — Kanboard's within-column ordering. Imported as-is; it is the only field whose
  meaning depends on Kanboard's board rendering, and after cutover it is just an ordering integer.
- `swimlane` — derived from the product for every row the current placement rule reaches;
  `product_lanes.py` exists because some rows predate it. The importer derives the lane from the
  product and keeps the observed lane in `extensions` where the two disagree, so
  `product_lanes.py`'s finding survives the migration instead of being silently normalized away.

### 8.7 Counters become aggregates

`sprint_budget` is a JSON object incremented read-modify-write, and `sprint_budget_uncharged` is a
second one beside it. As `sprint_budget_events` rows, `by_type`, `total`, `signal_reached` and
`hard_reached` are computed from the rows and the installation thresholds
(`instance.yaml: sprint_budget.signal = 12, hard = 30`). The importer creates one synthetic row per
counted event, with `occurred_at` taken from the sprint's audit journal where an event carries the
charge, and from the sprint's `updated_at` where it does not, marked in the report as
`occurred_at: approximate`. The totals reconcile exactly; only the per-event timestamps are
approximate for historical rows, and the report says so rather than presenting them as observed.

---

## 9. Where the stable identifiers live

| Identifier | Today | In the schema |
|---|---|---|
| `sprint:N` | `reference` on the sprint row; N allocated by `next_reference` over open **and** archived rows under `reference_allocation_lock` | `sprints.sprint_number` PK; `sprints.ref` a generated `UNIQUE` column. New numbers come from `sprint_number_seq`, set past the imported maximum at import. The unique index — not a file lock — is what makes reuse impossible, which is the defect `references.py` documents for 2026-08-06. |
| `<project>-<n>` task refs | `reference` on the card row; same allocator, same lock, same archived-rows rule | `tasks.task_ref` PK plus `UNIQUE (project_id, task_number)`; one sequence per project, or `max(task_number)+1` under the row lock the insert takes anyway. The 2026-08-18 defect (a reference derived from a fresh row id colliding with an archived card) cannot recur: archived rows are ordinary rows in `tasks`. |
| `product:<id>` | `product_id` metadata + `reference` | `products.product_id` PK, `products.ref` generated `UNIQUE` |
| `issue:<hash>` | `reference`, hash-allocated (not numbered) | `issues.issue_id` PK, `issues.ref` generated `UNIQUE` |
| sprint↔issue links | `sprint_issues` JSON array | `sprint_issues` table, FK both ways |
| sprint↔project links | `sprint_reservations` JSON array + derived guard index | `sprint_projects` (§4) |
| sprint↔repository links | `sprint_repositories` JSON array of paths | `sprint_repositories` → `repositories` (§8.3) |
| card→sprint | `sprint_ref` metadata | `tasks.sprint_number` FK |
| card→card dependency | `blocked_by` metadata | `task_dependencies` |
| supersession | `supersedes` metadata | `task_supersessions` |
| archive | Kanboard `is_active = 0`, `closeTask` | `tasks.archived`; the row is never deleted |
| issue close reason | `issue_closed_reason` metadata | `issues.close_reason` + the state CHECK |
| sprint close reason and account | close event payload + knowledge closeout | `sprints.close_reason`, `sprints.closeout_document` (the path), `sprint_decisions` (§8.5) |
| card decision (`release`/`rework`/`reslice`) | `[decision:*]` comment + `CARD_DECIDED` event | `board_events` + `task_comments.marker` |
| worker report and review verdict | `[report:*]` / `[review:*]` comments + events | same |
| budgets | `sprint_budget` JSON counters | `sprint_budget_events` + derived totals (§8.7) |
| `request_id` | `TaskAudit` committed/pending records, `ProductIssueTransaction` documents | `board_events.request_id UNIQUE`, `command_requests.request_id` PK, `sprint_budget_events.request_id UNIQUE`, `task_comments.request_id` / `sprint_comments.request_id UNIQUE` |
| `event_id` | `events.ndjson` records | `board_events.event_id` PK |
| head run reference | `Actor.head_run_ref` on events | `board_events.head_run_ref` (a reference only; the head registry stays out of this schema) |

Every reference in the left column survives cutover with the same spelling. Nothing in this schema
renumbers, rewrites or re-derives an existing `sprint:N` or task ref.

---

## 10. What this document does not decide

- The SQL migration files, the importer, the dry-run report format, the parity check and the
  cutover procedure. Separate cards.
- Containerization of the application, the web transport, the dispatcher or the workers, and the
  removal of Orca. A separate slice the owner has not approved.
- Any change to behaviour, UI, admission rules or project selection.
- Moving agent runtime, memory, profiles, secrets or providers.
- Any change to Kanboard or to the data now in it.
- Updates to `ARCHITECTURE.md`, `PROTOCOLS.md`, `OPERATIONS.md` and `TESTING.md`. Those are updated
  when the mechanism exists, not from a design document.
