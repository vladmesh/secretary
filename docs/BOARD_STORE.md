# The board store: read/write inventory and the PostgreSQL schema

Status: schema and container provisioning are implemented through revision
`0007_card_transport_key` for Cards, Product/Issue and Sprint. Live import, backend-aware backup
and the cutover controller are implemented; the external live operation remains pending and the
default backend is still Kanboard.

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
# (a) the protocol seam, as the seam names itself
grep -rln --include='*.py' -E '\bBoardHost\b|board_host' src   # 7 files

# (a2) the seam's only production adapter, named by callers that never name the Protocol
grep -rln --include='*.py' 'KanboardBoardHost' src             # 5 files

# (b) direct JSON-RPC clients
grep -rln --include='*.py' 'KanboardClient' src          # 23 files
grep -rn  --include='*.py' 'KanboardClient' src | wc -l  # 81 occurrences

# which RPC methods each module names
grep -ohE '"[a-z][A-Za-z]*(Task|Tasks|Comment|Comments|Metadata|Project|Projects|Column|Columns|Swimlane|Swimlanes|Version|Reference)[A-Za-z]*"' <file>

# the key-value domain model and the comment journal
grep -rn --include='*.py' -E 'get(TaskMetadata)|save(TaskMetadata)' src | wc -l   # 44 occurrences
grep -rn --include='*.py' -E '"(getAllComments|createComment)"' src | wc -l       # 32 occurrences

# (b2) INDIRECT reachers: modules that construct a board reader or writer and thereby
#      reach Kanboard without ever naming KanboardClient
grep -rn --include='*.py' -E '\b(TaskReader|TaskWriter|SprintReader|SprintWriter|ProductIssueStore)\(' src

# (b3) the wider net: every module that names one of those types or KanboardClient at all.
#      A superset of (b) and (b2); its extra hits are filtered by hand — see caution 4.
grep -rln --include='*.py' -E '\b(TaskReader|TaskWriter|SprintReader|SprintWriter|ProductIssueStore|KanboardClient)\b' src

# (c) consumers of the derived exports. The group is defined by the data flow below;
#     these three searches corroborate it and are what a re-run repeats.
# (c1) the export by its module and pathspec names
grep -rln --include='*.py' 'normalized_checkpoint\|validated_normalized_cards\|cards.ndjson\|export_board\|state/board' src   # 17 files
# (c2) the export by the artefact filenames themselves
grep -rln --include='*.py' -E 'cards\.json|cards\.ndjson|sprints\.ndjson|export\.json|export\.ndjson|kanboard-raw' src        # 21 files
# (c3) hop 3: the archive that carries a copy of the export
grep -rln --include='*.py' -E 'backups|ARCHIVE_ROOT|requires_raw_board_data' src                                                 # 7 files
```

**No single search is a group.** Each group is a claim about a path; a search is only evidence for
it, and every group here needed more than one search before its hits and its rows reconciled.

- `(a)` alone is not group (a). It finds the modules that name the `Protocol` or hold a
  `board_host` attribute; it does not find `sprints.py`, which imports `KanboardBoardHost` directly
  and drives sprint transitions through it (`sprints.py:846-853` builds the host,
  `_transition_host` at `:872` calls `.transition(...)` on it).
  `(a2)` is what finds that one. Symmetrically, three of `(a)`'s seven hits are not paths at all
  (caution 6).
- `(b)` alone is not the inventory, and treating it as one is how the first draft of this document
  missed two modules. `(b)` finds modules that build or type a JSON-RPC client. A module that is
  *handed* a reader, or that builds one from a client somebody else made, reaches Kanboard just as
  much and matches nothing in `(b)`. `(b2)` is what finds those; `(b3)` is the deliberately loose
  net whose false positives caution 4 removes.
- `(c)` is not a grep at all. It is enumerated by **data flow**, and the greps corroborate the
  enumeration rather than define it — see immediately below, and caution 7 for the three rounds in
  which grepping for it silently lost the same two modules.

The union of `(a)`, `(a2)`, `(b)`, `(b2)` and the hand-filtered remainder of `(b3)` is groups (a)
and (b); the data flow below is group (c). §2.5 records what each search contributed and shows the
arithmetic closing.

**Group (c), enumerated by data flow.** The derived export is not one file in one place. It is
copied forward through three hops, and a module belongs to group (c) when it reads, validates,
copies, publishes or deletes the export at *any* hop:

| Hop | Artefact | Written by |
|---|---|---|
| 1 | `<data>/board/**` — `cards.json`, `cards.ndjson`, `sprints.json`, `sprints.ndjson`, `events.ndjson`, `audit.json`, `audit.ndjson`, `export.json`, `analytics-manifest.json`, and the `kanboard-raw-*/` dump | `data.py` (`export_board`, `export_all`, `raw_kanboard_dump`) |
| 2 | `<instance>/state/board/**` — the published, git-committed copy of hop 1 | `checkpoint.py` through `state_repo.py`, under `BOARD_RUNS_PATHSPEC` |
| 3 | `<data>/backups/secretary-backup-{core,full}-<ts>.tar` — an archived copy of hop 1 under `archive/secretary-data/board/**` | `backup.py` |

Hop 3 is why a grep-defined group (c) was wrong. A hop-3 consumer addresses the export through the
tar, so it names `archive/secretary-data/board/cards.json`, or a component policy, or nothing but
the archive's own filename — and matches none of the module and pathspec names search `(c1)`
carries. Enumerating the flow instead puts `backup_verify.py` and `backup_retention.py` in the
group where they always belonged, and `(c3)` is the search that repeats that result.

Seven cautions the inventory depends on, and which any later re-run must respect:

1. `grep -r` honours `.gitignore`. Run it over `src/` in a clean checkout; a "nowhere in the tree"
   conclusion from a recursive grep is not evidence about ignored paths.
2. Counting `KanboardClient` counts *construction and type references*, not calls. A module that
   takes a client as a parameter (`status.py`, `restore.py`, `product_lanes.py`) reaches Kanboard
   even where it never builds one.
3. A module reaches Kanboard through `TaskReader`/`TaskWriter`/`SprintReader`/`SprintWriter`/
   `ProductIssueStore` even when it never names `KanboardClient`. `(b2)` finds the ones that
   construct such an object — `webproto/admission.py` and `dispatcher_production.py` are found only
   here. It does *not* find the ones that receive one from a factory: `board/steward_reports.py`
   and `board/done_retention.py` take a `board_factory`/`reader_factory` callable and are added by
   hand from their imports.
4. **A type name in a comment or docstring is not a path.** `(b3)` returns 37 files; nine of them
   (`dispatcher_helpers.py`, `dispatcher_observer.py`, `dispatcher_watchdog.py`,
   `webproto/command_reads.py`, `webproto/commands.py`, `webproto/sprint_requests.py`,
   `webproto/__init__.py`, `web/app.py`, `board/card_transitions.py`) and
   `triggered_agents/runtime/redact.py` match only in prose explaining what some other module
   does. They are named here so a later re-run recognizes them as filtered rather than forgotten,
   and does not re-add them.
5. **Group membership is not exclusive.** A module can be both a direct Kanboard path and a
   consumer of a derived export; `restore.py`, `installation.py`, `data.py`, `bootstrap.py`,
   `status.py` and the three `webproto` readers are exactly that. §2.3 marks dual membership
   instead of pretending its members never write.
6. **Defining a type is not reaching the board.** Three of `(a)`'s seven hits define or re-export
   the seam rather than travel it: `board/host.py` is the `Protocol` declaration,
   `board/fake.py` is `FakeBoardHost`, a test double never constructed in production, and
   `board/__init__.py` is the package surface — a docstring, an `__all__` and a lazy
   `__getattr__` that imports `KanboardBoardHost` on demand (`:56-59`). They are named here for
   the same reason as caution 4: so a re-run recognizes them as filtered rather than forgotten.
7. **A search over artefact names both over- and under-shoots group (c).** It undershoots at hop 3
   (above). It also overshoots, because two unrelated file sets share the export's filenames:
   `<data>/runs/cards.json` (run state, read by `board/reference_repair.py:70` and discussed in
   `triggered_agents/agents/steward/signals.py:425-453`) and the memory export's
   `export.json` / `export.ndjson` (`memory_journal.py`, `memory_reindex.py`, `memory_service.py`,
   `upgrade.py`). Neither set is the board export; §2.5 names all six as filtered.

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
| `sprints.py` (`SprintWriter._transition_host`) | Sprint lifecycle transitions only (`open`→`closed`/`stopped`/`open`) | W | no — found only by `(a2)` |
| `product_issues.py` | Product/Issue create, priority replace, close, via the host | R+W | no |
| `dispatcher.py` | reads `self.writer.board_host.canon` for events, attempt usage and outcome occurrences | R | yes (`secretary-dispatcher-production.service`) |

Four of these five rows come from search `(a)`. `sprints.py` comes only from `(a2)`: it never
writes `BoardHost` or `board_host`, it imports the concrete `KanboardBoardHost` in `_host`
(`:846-853`, lazily, to keep reader imports acyclic) and `_transition_host` (`:872`) calls
`.transition(...)` on what it returns. A group defined by the `Protocol`'s own name would have
lost every sprint lifecycle transition, which is why `(a2)` is declared alongside `(a)` rather
than left as a hand-added row.

What the seam does **not** carry today: Sprint create and close bodies, Sprint metadata edits
(goal, DoD, repositories, reservations, current task, resume, budget), Card create, Card edit, Card
claim/routing metadata, Card archive, ordinary role comments, swimlane placement, board bootstrap
and every restore path. `KanboardBoardHost._migration_pending` is the explicit refusal that marks
those as unmigrated.

### 2.2 Group (b): direct `KanboardClient` / JSON-RPC

Since `secretary-1587` the first three rows of this table have **two** implementations, and which
one a process uses is read from one named place — `SECRETARY_CARD_BACKEND`, `kanboard` (the
default) or `postgres` — by `board/backend.py`.  The decision is taken once per process, is
reported by `secretary status` under `card_backend`, and an unknown value refuses rather than
falling back.  Nothing about it is inferred from whether `board-store.env` exists: an installation
may hold a fully migrated store and still be served by Kanboard, which is the state the live
installation is in.

The seam is *underneath* `TaskReader` and `TaskWriter` rather than beside them, for the reason
this section exists: almost every consumer below reaches cards through those two classes, so one
replacement under them moves the CLI, the web process, the dispatcher and the observer together.
`board/sql_cards.py` answers the same board vocabulary over cards, Product/Issue and Sprint rows and
comments, and `board/sql_audit.py` is `TaskAudit`'s contract over `requests` and `board_events`
(§7.3). `ProductIssueStore`, `SprintReader` and `SprintWriter` follow the same process-wide choice.

**Where the switch is acted on.**  `board/backend.py:board_client` is the only function in
`secretary` that constructs a board client, and every entry point in the two tables below reaches
its client through it.  It takes `serves` — what the call site needs from the client, out of
`card`, `sprint` and `product/issue` — because the second implementation holds only cards.  Three
things follow, and each is a property the switch would not have if a consumer named a backend for
itself:

* an unknown `SECRETARY_CARD_BACKEND` refuses at **every** entry point, not only at the ones
  somebody remembered to check;
* under `postgres`, Product/Issue-only sites such as `product_issue_commands.py` receive the SQL
  client; sites that also need sprints receive that same SQL client, while an unknown capability
  is refused by name instead of being handed a client that contradicts the switch;
* the refusals leave as `TaskError`, the vocabulary every command already renders as a named
  failure with an exit status, so a missing or malformed `board-store.env` (`BoardStoreError`) and
  an unreachable server (`psycopg`) are diagnoses rather than tracebacks.

Exactly two modules still name Kanboard directly, and they say so in their own source rather than
by default.  `bootstrap.py` creates the Kanboard service's own board, columns and swimlanes and
waits for the container to answer `getVersion`: the store has no equivalent to create, so reading
the switch there would be a switch with one branch.  `board/import_board.py` reads the Kanboard
board it copies *into* the store, so consulting the switch would ask the destination to be the
source.  `tests/test_architecture.py` holds that list and its reasons executably: a third module
that builds a client for itself fails the unit suite.

**The identity a normalized row carries.**  `<kind>_<backend>_<n>` — `task_kanboard_12`,
`task_postgres_37`, `sprint_kanboard_9` — is one convention, minted by `board/backend.py:
entity_id` and read back by `entity_number`, and by nothing else.  `kind` is the entity, `backend`
is the implementation that answered, and `n` is that implementation's own number for the row:
Kanboard's task id, or `tasks.board_key` in the store. `tasks.task_ref` remains the stable public
identity and `tasks.task_number` remains its per-project public number; neither is a globally unique
integer protocol address. One function each way is not
tidiness.  Every consumer that stripped a single literal prefix answered "invalid" for every row
the other backend produced, which is how `report`, `verdict` and `decide` refused on the
PostgreSQL backend — through `BoardHost.marker_comment`'s card lookup — while the reader that had
just minted the identity worked; `sprints.py`'s sprint lookup carried the same defect with
`sprint_kanboard_`.

Cards, Products and Issues retain their string references but the inherited board vocabulary addresses a
row by integer. `board.backend.record_key` hashes `<kind>:<identifier>` into disjoint positive
`bigint` ranges above the card schema's `int4`; the value is stored as each row's unique indexed
`board_key`. Card keys are sequence-allocated below two billion. Lookups use the appropriate unique
index and reject malformed or colliding stored keys. A collision is refused,
never resolved by scanning or guessing, and Product, Issue and Card keys cannot cross kinds.


`KanboardClient` (`src/secretary/tasks.py:469`) is a generic JSON-RPC client with `call`,
`call_batch` (chunked, `_BATCH_CHUNK = 200`) and byte-size preflight. It is constructed from
`board-transport.env` (`KANBOARD_URL`, `KANBOARD_API_USER`, `KANBOARD_API_TOKEN`) via
`board_transport.resolve`.

**Core domain modules**

| Module | Entities and fields | R/W | Own writer process? |
|---|---|---|---|
| `tasks.py` — `TaskReader` | Cards: `reference`, `title`, `description`, column→`state`, `is_active`→`closed`, `position`, swimlane; metadata `project`, `task_type`, `blocked_by`, `claim`, `slug`, `base_branch`, `seed_ref`, `supersedes`, `head`, `resolved_head`, `review_head`, `resolved_review_head`, `retry_same`, `retry_switch`, `retry_heads`, `complexity`, `family_preference`, `routing_reason`, `quota_snapshot_at`, `codex_launch_mode`, `sprint_ref`, `record_type`; all comments | R | no — used by every process below |
| `tasks.py` — `TaskWriter` | Same, plus `createTask`, `updateTask`, `moveTaskPosition`, `closeTask`, `saveTaskMetadata`, `createComment`; `_READY_RESET_METADATA` clears `claim`/`resolved_head`/`resolved_review_head`/`retry_*` on a Ready transition | R+W | no |
| `tasks.py` — `TaskAudit` | Kanboard backend only: the local append-only journal `<data>/board/events.ndjson`, pending records `<data>/board/pending-audit/v2-<sha256>.json`, lock `<data>/board/.audit.lock` | R+W (files) | no |
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

**Indirect reachers — found only by search `(b2)`**

Neither of these names `KanboardClient`. Both construct a board reader or writer from a client
they were handed, and both therefore reach Kanboard on a live path.

| Module | Entities and fields | R/W | Own writer process? |
|---|---|---|---|
| `webproto/admission.py` (`admit`, `_card` at `:122-125`) | `TaskReader(board).show(ref)` — one Card: `ref`, `project`, `state`, and through `_refuse_reserved_project` the derived reservation index `<data>/sprints/active-repositories.json`. The single gate `run_start` and `run_review` both pass before any workspace or head exists. | R | yes — inside `secretary-web.service`, on every product-run start |
| `dispatcher_production.py` (`_reconcile_sprint_budget` at `:1372-1380`) | Constructs `SprintWriter(runtime.reader.client, data_dir=…, thresholds=…)` and charges each durable card event once, using the audit identity as the budget request id: writes `sprint_budget` / `sprint_budget_uncharged` metadata on sprint rows, reads `runtime.audit.events()` | R+W | **yes** — inside `secretary-dispatcher-production.service`, every 60 s tick |

The previous draft covered the second only by a `dispatcher_*.py family` bullet in §2.4, which
supplies none of the columns AC2 requires. Both now have rows. Neither belongs in §2.6: a module
found late by a better search is inventory, not a gap.

### 2.3 Group (c): consumers of derived exports

A module is in this group when it reads, validates, copies, publishes or deletes the derived
export at any of §1's three hops — `<data>/board/**`, `<instance>/state/board/**`, or the copy
inside `<data>/backups/secretary-backup-*.tar`. The group is the data flow, not a grep; the greps
`(c1)`, `(c2)` and `(c3)` corroborate it and §2.5 reconciles all three.

Membership is **not** exclusive: `restore.py`, `installation.py`, `data.py`, `bootstrap.py`,
`status.py` and the three `webproto` readers are in group (b) as well, and the "also (b)" column
says so rather than leaving the reader with a contradiction. What is true of every member is only
this: *the export path itself* never speaks to Kanboard.

The **Hop** column says which copy of the export the module touches, so a later re-run can check the
group against the flow rather than against a single search.

| Module | Hop | Consumed fields | R/W | Own writer process? | Also (b)? |
|---|---|---|---|---|---|
| `checkpoint.py` | 1→2 | regenerates the exports, validates, publishes `state/board` + `state/runs` under `BOARD_RUNS_PATHSPEC`; seals `analytics-manifest.json`; reads `runs.ndjson` to refuse history loss | R+W (files, git) | yes — runs inside the dispatcher tick, and as `secretary checkpoint` | no — it reaches the board only through `data.py:export_board` |
| `data.py` | 1 | **produces** `cards.json`, `cards.ndjson`, `sprints.json`, `sprints.ndjson`, `audit.json`, `audit.ndjson`, `export.json`, validating the pair before publishing; **reads** the previous `<data>/board/kanboard-raw-*/data/db.sqlite` for `raw_active_task_count` (`_latest_raw_active_task_count`) | R+W | yes (checkpoint/backup) | **yes** — full group (b) reader; see §2.2 |
| `bootstrap.py` | 2 | reads `<instance>/state/board/cards.ndjson` and takes each card's `swimlane` to seed lane discovery before it writes the Pipeline board (`:91-103`) | R | yes (bootstrap command, root) | **yes** — full group (b) writer; see §2.2 |
| `board/normalized_checkpoint.py` | 1 | `cards.json` / `cards.ndjson` and their parity; card `reference` uniqueness; Product/Issue record validity | R | no — a validator inside its caller | no |
| `board/analytics.py` | 2 | a sealed, copied `state/board`: `cards.ndjson`, `sprints.ndjson`, `events.ndjson`, `audit.ndjson`, `analytics-manifest.json` | R | no — offline, takes a directory rather than an installation | no |
| `backup.py` | 1→3 | `board/cards.json`, `cards.ndjson`, `export.json`; filters Done cards out of a `core` archive (`_filter_core_board_export`) | R+W (tar under `<data>/backups`) | yes (backup command) | indirectly — it calls `data.py:export_all` and `raw_kanboard_dump` |
| `backup_policy.py` | 3 | declares which board entries each policy requires; reads nothing itself | — | no — a policy table | no |
| `backup_verify.py` | 3 | the export **inside the archive**: `archive/secretary-data/board/cards.json` → `cards[].reference` \| `cards[].id` and `cards[].column`, to fail a `core` archive that carries a Done card (`:265-278`); the `raw_board` component directory → `manifest.json` and any file under `data/**` (`_verify_raw_board_component`, `:235-247`); the archive paths `archive/secretary-data/board/kanboard-raw-*`, to fail a `core` archive carrying a raw dump (`:252-259`); component names, paths and checksums from `archive/versions.json` | R (tar) | yes — `secretary backup verify` is its own short-lived operator process (`cli.py:1515`); also a library inside `restore.py` (`_verify_plain_tar`) and re-exported by `backup.py` | no |
| `backup_retention.py` | 3 | no card field: it consumes the export at archive granularity — the names of `<data>/backups/secretary-backup-(core\|full)-<UTC ts>.tar`, the kind parsed from the name, the timestamp parsed from the name (file `mtime` as fallback) — and **deletes** archives past `policy.retention_seconds`, keeping only the newest where retention is `None`. It decides how long each archived copy of the board export survives | R (names, mtimes) + W (deletes archives) | no — a library called inside the `secretary backup` process (`backup.py:153`), which is itself that process's own writer | no |
| `restore.py` | 2, 3 | the checkpoint's `sprints.ndjson` / `cards.ndjson` through `validated_normalized_cards` (hop 2); when restoring from an archive, the same export through `backup_verify._verify_plain_tar` (`:27`, hop 3) | R, then **W to Kanboard**: `addSwimlane`, sprint rows via `SprintWriter`, cards via `TaskWriter` / `task_restore.py` | yes (restore command) | **yes** — full group (b) writer; see §2.2 |
| `installation.py` | 2 | `CHECKPOINT_BOARD` = `cards.ndjson`, `sprints.ndjson`, `events.ndjson`, `export.json`, to build a new local data plane | R, then **R from Kanboard**: `TaskReader(...).list()` to verify | yes (install/recovery command) | **yes** — group (b) reader; see §2.2 |
| `state_repo.py` | 2 | owns `BOARD_RUNS_PATHSPEC` = `state/board`, `state/runs`; stages and commits it | W (git) | no — a library under the tick writer's lock | no |
| `status.py` | 1 | `<data>/board/cards.ndjson` as the age/evidence fallback when the live read fails | R | yes (operator command) | **yes** — group (b) reader first |
| `webproto/reads.py`, `webproto/sprint_reads.py`, `webproto/pause_reads.py` | 1 | the same `cards.ndjson` as each section's availability evidence | R | yes — inside `secretary-web.service` | **yes** — all three are group (b) readers first |
| `cli.py` | 1, 3 | wires `data export-board` (hop 1) and `backup create` / `backup verify` (hop 3, `:270`, `:1497`, `:1515-1516`); consumes no field itself | — | no — it is the argument surface of the process that does the consuming | no |

### 2.4 Consumers named by DoD 3, explicitly

- **CLI** — `task_commands.py`, `sprint_commands.py`, `product_issue_commands.py`, `cli.py`,
  `check_commands.py`, `restore_commands.py`, `host_commands.py`, `dispatcher_commands.py`.
  Group (b), one process per invocation.
- **web** — `web/server.py`, `web/app.py`, `web/pages.py`, `webfront/*` over `webproto/*`.
  Group (b), one long-lived threaded process. Its board-touching members each have their own row
  in §2.2: `reads.py`, `ops.py`, `sprint_reads.py`, `sprint_ops.py`, `pause_reads.py`, and
  `admission.py` — the last of which reaches the board on every run start and was found only by
  search `(b2)`.
- **dispatcher** — one oneshot process per 60 s tick. Its board-touching members are named
  individually rather than as a family, because a family bullet carries none of AC2's columns:
  `dispatcher.py` (`runtime_from_args`, group (a)+(b)) and `dispatcher_production.py`
  (`_reconcile_sprint_budget`, group (b) through `SprintWriter`) both have rows in §2.2, and
  `dispatch/standing_agent.py` has its own. Every other `dispatcher_*.py` module in the tree
  operates on the runtime those two build and reaches the board only through them; the three that
  name a board type at all (`dispatcher_helpers.py`, `dispatcher_observer.py`,
  `dispatcher_watchdog.py`) do so only in prose, per caution 4.
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

### 2.5 What each search contributed, and the arithmetic that closes it

An inventory whose counts do not reconcile is not an inventory: "17 files, 12 rows" says nothing
about the other five, and that is how a missing consumer survived three rounds of review. So every
hit of every declared search is accounted for here as **either a table row or a named filtered
entry with its reason**, and each line adds up.

| Search | Hits | Rows | Filtered | Reconciles |
|---|---|---|---|---|
| `(a)` `BoardHost` / `board_host` | 7 | 4 | 3 | ✓ |
| `(a2)` `KanboardBoardHost` | 5 | 4 | 1 | ✓ |
| `(b)` `KanboardClient` | 23 | 22 | 1 | ✓ |
| `(b2)` reader/writer construction | 19 | 19 | 0 | ✓ |
| `(b3)` any mention of a board type | 37 | 27 | 10 | ✓ |
| `(c1)` export module and pathspec names | 17 | 15 | 2 | ✓ |
| `(c2)` export artefact filenames | 21 | 15 | 6 | ✓ |
| `(c3)` hop 3, the archive carrying the export | 7 | 7 | 0 | ✓ |

`Rows` counts the hits of *that* search which are table rows; the same row is reached by several
searches, so the column does not sum down the table. §2.1 prints 5 rows; §2.2 prints 28 rows over
26 modules (`tasks.py` gets three, one per class); §2.3 prints 15 rows over 17 modules (the three
`webproto` readers share one).

**`(a)` — 4 rows + 3 filtered.** Rows: `board/kanboard.py`, `tasks.py`, `product_issues.py`,
`dispatcher.py`. Filtered, per caution 6: `board/host.py` (the `Protocol` definition itself),
`board/fake.py` (`FakeBoardHost`, a test double never constructed in production) and
`board/__init__.py` (the package surface: docstring, `__all__`, and a lazy `__getattr__` that
imports the adapter on demand).

**`(a2)` — 4 rows + 1 filtered.** Rows: `board/kanboard.py` (it *is* the adapter), `tasks.py`,
`product_issues.py` and **`sprints.py`**, which no other declared search reaches. Filtered:
`board/__init__.py`, again for caution 6. The previous revision claimed `(a)` returned 5 rows and
2 filtered; running `(a)` returns seven files that do not contain `sprints.py`, so those numbers
could not both be true. §2.1 keeps all five rows and `(a2)` is what earns the fifth.

**`(b)` — 22 rows + 1 filtered.** All 23 files appear in §2.2 except `webproto/__init__.py`, whose
only match is the package docstring explaining that the layer reaches the board through
`KanboardClient`. Filtered as prose.

**`(b2)` — 19 files, all accounted for.** Seventeen were already rows from `(a)`, `(a2)` or `(b)`;
the two that were not are `webproto/admission.py` and `dispatcher_production.py`, which is exactly
what this search exists to catch. Nothing is filtered here: constructing a reader or writer *is* a
path.

**`(b3)` — 27 rows + 10 filtered.** The 27 are the union of `(a)`, `(a2)`, `(b)` and `(b2)` plus
`board/reference_repair.py`, `board/steward_reports.py` and `board/done_retention.py` — the last
two receive a `board_factory` / `reader_factory` callable instead of constructing one, so they
match only `(b3)`. The 10 filtered are prose-only matches, named individually in caution 4 so a
later re-run does not re-add them: `dispatcher_helpers.py`, `dispatcher_observer.py`,
`dispatcher_watchdog.py`, `webproto/command_reads.py`, `webproto/commands.py`,
`webproto/sprint_requests.py`, `webproto/__init__.py`, `web/app.py`, `board/card_transitions.py`,
`triggered_agents/runtime/redact.py`.

**`(c1)` — 15 rows + 2 filtered.** Fifteen of the 17 hits are §2.3 rows: `checkpoint.py`,
`data.py`, `bootstrap.py`, `board/normalized_checkpoint.py`, `board/analytics.py`, `backup.py`,
`backup_policy.py`, `restore.py`, `installation.py`, `state_repo.py`, `status.py`,
`webproto/reads.py`, `webproto/sprint_reads.py`, `webproto/pause_reads.py`, `cli.py` — counting the
three `webproto` modules that share one printed row as three. Filtered: `knowledge_write.py`
(module docstring, explaining that the tick writer commits `state/board`/`state/runs` in the same
repo every minute) and `memory_write.py` (an inline comment saying a concurrent
`state/board`/`state/runs` commit neither blocks nor is blocked by a memory write). Neither reads a
board file; both mention the pathspec while explaining lock behaviour.

**`(c2)` — 15 rows + 6 filtered.** The rows are §2.3's 17 modules minus two that name no artefact
filename: `state_repo.py` (it names the pathspec) and `backup_retention.py` (it names only the
archive). The other six hits share a filename with the board export without touching it, per
caution 7. Filtered, each with its file set: `board/reference_repair.py:70` and `triggered_agents/agents/steward/signals.py:425-453` —
`<data>/runs/cards.json`, the run-state companion, not the board export (`reference_repair.py` is a
§2.2 row for its Kanboard writes, and this hit is not what puts it there); `memory_journal.py:396`,
`memory_reindex.py:32`, `memory_service.py:47`, `upgrade.py:570` — the *memory* export's
`export.json` / `export.ndjson`. Six filtered, and 15 + 6 = 21.

**`(c3)` — 7 rows + 0 filtered.** `backup.py`, `backup_policy.py`, `backup_verify.py`,
`backup_retention.py`, `restore.py`, `data.py` (`LAYOUT_DIRS` creates `<data>/backups` and `:815`
excludes it from the export it produces) and `cli.py` (the `backup` subcommands). Two of the seven
— **`backup_verify.py` and `backup_retention.py`** — have no row in any other search, and this is
the search that was missing when they fell out of the inventory three times running.

**Why they fell out, stated so it does not recur.** Group (c) was defined by `(c1)`, a search over
the export's *module and pathspec* names. `backup_verify.py` addresses the export through the
archive (`archive/secretary-data/board/cards.json`) and `backup_retention.py` addresses it through
the archive's filename alone; neither writes `cards.ndjson`, `state/board`, `export_board`,
`normalized_checkpoint` or `validated_normalized_cards` anywhere. Three rounds of re-running the
same search reproduced the same absence, because the search was the definition. §1 now defines the
group by data flow and gives hop 3 its own search, which is the repair for the class rather than
for the two instances.

Six printed §2.3 rows, eight modules, are dual-membership: `data.py`, `bootstrap.py`,
`restore.py`, `installation.py`, `status.py` and the three `webproto` readers are direct Kanboard
paths *and* derived-export consumers, so each has a row in both §2.2 and §2.3. `bootstrap.py` is the clearest case and the one
that shows why the "never speaks to Kanboard" framing had to go: it reads
`state/board/cards.ndjson` for lane discovery **and then creates the Pipeline board**, in that
order, in one command.

The first draft of this document ran only `(a)`, `(b)` and `(c1)` and missed what `(b2)` finds. The
second gave `(c)` no per-row columns and no arithmetic. The third still counted `(a)`'s hits as if
`sprints.py` were among them and still defined `(c)` by a grep. The table above is the fix for the
class: a search whose hits do not equal rows plus named filtered entries is not finished, and a
group whose definition *is* a search cannot discover what the search cannot spell.

### 2.6 Gaps, listed as gaps

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
row has no natural key. Every reference between entities is a real foreign key, and a reference
that is only meaningful *within* one sprint is a **composite** foreign key carrying the sprint —
never a bare existence check (§3.3, §3.4, §3.8). Closed vocabularies are `CHECK` constraints, by
the uniform rule of §3.12. `jsonb` appears in exactly seven places, each justified inline (§3.10).

**What the first import of real data changed here (2026-09-07).** `secretary-1583` ran the
importer against the whole live board — 1513 Pipeline rows with 15 571 comments, 102 sprint rows —
and named every record this schema could not carry. Five of its findings are answered below and
each is marked where it lands: `issue_comments` (§3.7, 479 comments on Issue rows had no table),
`issues.extensions` (§3.2, nine metadata keys on 158 Issue rows had no home), the sprint's identity
(§3.3 and §9, two live sprints have no number in their reference), a nullable `tasks.project_id`
(§3.5, one card carries no `project` metadata) and `task_dependencies.depends_on_task` (§3.5, nine
`blocked_by` values name cards that are not on the board). Revision `0002_board_gaps` is exactly
that list; §3.13 carries both revisions' catalogue numbers.

The DDL below is written entity by entity for reading, not in executable order. Several references
are forward or mutual — `sprints.current_task_ref` → `tasks`, `sprints.resume_id` ↔
`sprint_resumes`, `sprint_budget_events` and `sprint_decisions` → `tasks`, and every
`request_id` → `requests` — so the migration that creates them adds those constraints with
`ALTER TABLE … ADD CONSTRAINT` after both tables exist, exactly as shown. The sprint's two cursors
are additionally `DEFERRABLE INITIALLY DEFERRED`, because a sprint row and the card or resume it
points at are inserted in one transaction (§7.1).

### 3.1 Products, projects, repositories

```sql
CREATE TABLE products (
    product_id   text PRIMARY KEY,                    -- "secretary"; matches ^[a-z0-9][a-z0-9-]{0,62}$
    board_key    bigint NOT NULL UNIQUE,              -- stable indexed adapter key; see §9
    ref          text GENERATED ALWAYS AS ('product:' || product_id) STORED UNIQUE,
    title        text NOT NULL CHECK (title <> ''),
    description  text NOT NULL DEFAULT '',
    state        text NOT NULL DEFAULT 'active' CHECK (state IN ('active','archived')),
    extensions   jsonb NOT NULL DEFAULT '{}'::jsonb, -- (J7); unknown metadata provenance
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

`products.extensions.kanboard` preserves every metadata key outside the known Product columns and
the relational `product_projects` set. The adapter filters known keys before merging this bag, so
provenance cannot replace canonical identity or relationships.

`projects` and `repositories` are present "to the extent of existing links" only: the registry file
stays canonical for the binding (§6), and these tables exist so that `sprint_projects.project_id`,
`tasks.project_id` and `sprint_repositories.repository_id` can be real foreign keys instead of free
text. `registry_present = false` is how a historical row survives when its `.yaml` was deleted.

**These two tables are a projection of the file registry, and the projection needs a writer.**
Without one they are a mirror nobody updates, and the first ordinary project addition after cutover
turns from an admitted operation into a foreign-key failure: `registered_projects()` reads
`<instance>/projects/*.yaml` directly (`product_issues.py:124-140`), sprint admission accepts a
newly added id from it (`sprints.py:1506-1508`), and the write that follows would insert a
`tasks.project_id` or `sprint_projects.project_id` with no `projects` row behind it. §8.3 only
described the *import*; that is the gap, and it is closed here.

**The writer is one function, `board_store.sync_project_registry(instance, conn)`**, and it runs at
two points:

1. **Inside the mutation's own transaction**, immediately before any insert that references a
   project id. It reads the registry files — which are still canonical — and upserts the rows the
   mutation is about to reference:

   ```sql
   INSERT INTO projects (project_id, enabled, plane, adapter, orca_binding, registry_present)
        VALUES (:id, :enabled, :plane, :adapter, :orca_binding, true)
   ON CONFLICT (project_id) DO UPDATE
       SET enabled = EXCLUDED.enabled, plane = EXCLUDED.plane, adapter = EXCLUDED.adapter,
           orca_binding = EXCLUDED.orca_binding, registry_present = true;

   INSERT INTO repositories (project_id, path, remote, default_branch, role)
        VALUES (:project_id, :path, :remote, :default_branch, 'primary')
   ON CONFLICT (path) DO UPDATE
       SET project_id = EXCLUDED.project_id, remote = EXCLUDED.remote,
           default_branch = EXCLUDED.default_branch;
   ```

   Because it is in the same transaction, a first write against a freshly added `projects/new.yaml`
   succeeds exactly as it does today. Behaviour does not change, which DoD 2 requires: admission
   still reads the files and still decides, and this adds no rule of its own.

2. **In `secretary reconcile apply` and `secretary upgrade`**, over the whole registry, so a
   binding that was edited but not yet referenced by any write is projected anyway and an operator
   can see the table agree with the files.

**A binding the operator removes is never deleted from the database.** History references it —
`tasks.project_id`, `sprint_projects.project_id` on closed sprints — and deleting the row would
either fail on those foreign keys or lose records, which §8.3's rule forbids. Reconciliation sets
`registry_present = false` for every id it no longer finds in the files, and nothing else. A
re-added binding flips it back to `true` on the next projection. `enabled` mirrors the file for
whichever ids the file still has; for an absent binding it keeps its last value and
`registry_present = false` is the field that says the value is stale.

**This is not a second canon and not a second writer of the registry.** Nothing writes
`projects/*.yaml`; the operator does, as §6.2 says. The database rows are derived, one function
produces them, and every consumer that asks "is this project registered?" keeps asking
`registered_projects()` — the files — exactly as it does now.

### 3.2 Issues

```sql
CREATE TABLE issues (
    issue_id     text PRIMARY KEY,                    -- the 20-hex suffix of issue:<id>
    board_key    bigint NOT NULL UNIQUE,              -- stable indexed adapter key; see §9
    ref          text GENERATED ALWAYS AS ('issue:' || issue_id) STORED UNIQUE,
    product_id   text NOT NULL REFERENCES products(product_id),
    title        text NOT NULL CHECK (title <> ''),
    description  text NOT NULL DEFAULT '',
    issue_kind   text NOT NULL
                   CHECK (issue_kind IN ('bug','feature','question','improvement')),
    priority     text NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open','closed')),
    close_reason text CHECK (close_reason IN ('resolved','invalid','duplicate','wont_do')),
    extensions   jsonb NOT NULL DEFAULT '{}'::jsonb, -- (J6), added 2026-09-07; see §8.2
    created_at   timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    CONSTRAINT issue_close_reason_matches_state
        CHECK ((state = 'closed') = (close_reason IS NOT NULL))
);
```

The last constraint is `product_issues.py:_validate_issue_record` made relational: today an open
issue with a close reason, or a closed one without, is caught by a Python validator at export time.

`extensions` is here because the 2026-09-07 import found nine leftover task-metadata keys riding on
158 Issue rows, and 72 Issue rows in a lane that is not their product's. A card keeps that kind of
provenance in `tasks.extensions`; an Issue had nowhere to keep it, so the keys would have been
dropped on import. §8.2's rule — and its per-key count in the importer's report — applies to this
column exactly as it applies to a card's.

### 3.3 Sprints

```sql
CREATE TABLE sprints (
    -- The identity is §9's stable reference, since 2026-09-07: two live sprints on this board are
    -- `sprint:canary-terra-20260813` and `sprint:canary-terra-final-20260813`, which an integer
    -- key cannot hold.  `sprint_number` stays for §9's numbering rule, as a nullable unique column.
    ref                text PRIMARY KEY,              -- "sprint:1037", "sprint:canary-terra-20260813"
    board_key          bigint NOT NULL UNIQUE,        -- disjoint board-client transport namespace
    sprint_number      integer UNIQUE,                -- N in sprint:N, NULL when the ref has none
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
    CONSTRAINT sprint_closed_has_time CHECK ((status = 'open') = (closed_at IS NULL)),
    CONSTRAINT sprint_ref_is_a_sprint_reference CHECK (ref ~ '^sprint:'),
    -- A numbered reference keeps exactly its number, and only a numbered reference has one.
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
    -- The target a scoped foreign key needs; redundant with the primary key by design.
    UNIQUE (resume_id, sprint_ref)
);
```

**Why the reference is the identity (2026-09-07).** The 2026-09-07 import found two live sprint
rows whose reference carries no number at all — `sprint:canary-terra-20260813` and
`sprint:canary-terra-final-20260813` — so `sprint_number integer PRIMARY KEY` could hold neither,
and `secretary-1438` and `secretary-1439` lost their link to a sprint with them. §9 already
promised that every reference survives cutover with the same spelling; making the reference the key
is what makes that true. Nothing else about §9 moves: `sprint_number_seq` still allocates the
number of a new numbered sprint, `sprint:N` is still spelled `sprint:N`, and the `UNIQUE` on
`sprint_number` is still what makes reuse of a number impossible. Every relation that was scoped by
`sprint_number` is now scoped by `sprint_ref` — the same composite construction, over an identity
that can hold every reference the board has. The reference that is still **not** representable is a
*duplicate* one: `sprint:1037` names two rows on today's board, a live one and an archived one, and
a primary key holds one of them. §9 records that.

The board-client integer namespace is central and disjoint: immutable Card `board_key` values are in
`[1,2000000000)` independently of public task numbers; numbered Sprints occupy
`[2000000000,2500000000)`, custom Sprint references occupy
`[2500000000,3000000000)`, Products `[3000000000,4000000000)`, and Issues
`[4000000000,5000000000)`. `sprints.board_key` makes dispatch an indexed equality lookup. No
metadata, comment, update or close path enumerates Sprint rows to guess an integer's owner.

`RESUME_FIELDS` in `sprints.py` is a fixed six-field tuple, so the resume is columns, not a blob.
Resume *freshness* is derived (`_resume_freshness`) and is not stored, exactly as
`docs/RECOVERY.md` already requires of the checkpoint.

**Scoped relations: a sprint's cursors point inside the sprint.**

A foreign key that only says "this task exists" is weaker than the writer it replaces.
`SprintWriter.set_current_task` (`src/secretary/sprints.py:1597-1604`) reads the card and refuses
with `"current task is not linked to this sprint"` when `task["sprint"] != reference`. A bare
`REFERENCES tasks(task_ref)` would let `UPDATE sprints SET current_task_ref = <a card of
sprint:2> WHERE sprint_number = 1` succeed, dropping an invariant the current product holds — the
opposite of DoD 1's requirement that links and checkable rules be relational.

So both cursors carry the sprint into the key:

Deferred constraints (they reference `tasks` and `sprint_resumes`; §3.13 places them):

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

Three properties make this work, and each is load-bearing:

- `tasks` carries `UNIQUE (task_ref, sprint_ref)` (§3.5) and `sprint_resumes` carries
  `UNIQUE (resume_id, sprint_ref)` purely so these composite keys have a target. Both are
  redundant with their table's primary key; that redundancy is the price of a scoped key and is
  cheaper than a trigger.
- `MATCH SIMPLE` is PostgreSQL's default and is **stated explicitly because it is the mechanism**,
  not an incidental default: a composite foreign key with any NULL column is not checked at all.
  That is exactly right here — a sprint with no current task (`current_task_ref IS NULL`) and a
  sprint before its first resume are both legal, and `ref` is the primary key and never NULL, so
  the check fires precisely when a cursor is set.
- `DEFERRABLE INITIALLY DEFERRED` is needed by normalized restore's cyclic order: Cards name their
  Sprint before that Sprint row exists, and the Sprint later names one of those Cards. Commit-time
  validation permits that transaction order, not an invalid committed state; the schema test commits
  a cross-Sprint cursor and proves PostgreSQL refuses it.

Moving a card between sprints now has a defined consequence rather than a silent one: the
`UPDATE tasks SET sprint_ref = …` fails while a sprint still names that card as its current
task, so the writer must clear the cursor first. That is the same refusal
`set_current_task` gives today, arriving from the other direction.

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

Deferred constraint (it references `tasks`; §3.13 places it):

```sql
ALTER TABLE sprint_budget_events
  ADD CONSTRAINT budget_card_is_in_this_sprint
      FOREIGN KEY (task_ref, sprint_ref)
      REFERENCES tasks (task_ref, sprint_ref) MATCH SIMPLE;
```

Today `sprint_budget` is a counter object in one metadata value, incremented read-modify-write. As
rows, the totals in `by_type`, `total`, `signal_reached` and `hard_reached` become an aggregate
over this table against the installation thresholds — derived, so the two can no longer disagree.
Idempotency of a retried charge comes from the request-ownership row of §3.9, not from a
compare-and-swap on a JSON blob. Interactive charges have one occurrence per request; restore may
replay several exported occurrences under the one restore request, so `request_id` is deliberately
not unique on this evidence table.

The scoped foreign key is the same construction §3.3 applies to the sprint's cursors, and it is
here for the same reason: `dispatcher_production.py:_reconcile_sprint_budget` resolves the sprint
*from the card* before it charges, so a charge naming a card that is not in that sprint is a
defect, not a variation. `task_ref` stays nullable — an `infrastructure_blocked` charge need not
name a card — and `MATCH SIMPLE` skips the check exactly then.

### 3.5 Cards (tasks)

```sql
CREATE TABLE tasks (
    task_ref       text PRIMARY KEY,                  -- "secretary-1580"
    board_key      bigint NOT NULL UNIQUE DEFAULT nextval('card_board_key_seq'),
                                                     -- immutable protocol identity, 1..1999999999
    -- Nullable since 2026-09-07: `secretary-583` carries no `project` metadata (§8.6).
    project_id     text REFERENCES projects(project_id),
    task_number    integer NOT NULL,
    title          text NOT NULL CHECK (title <> ''),
    description    text NOT NULL DEFAULT '',
    -- Nullable since 2026-09-07: `secretary-583` carries no `task_type` metadata either (§8.6).
    -- The vocabulary is still closed; only the absence of a value was added to it.
    task_type      text CONSTRAINT task_type_is_a_known_type_or_nothing
                     CHECK (task_type IS NULL OR task_type IN ('code','research')),
    state          text NOT NULL CHECK (state IN
                     ('issues','ready','in_progress','validate','assessment','blocked','done')),
    archived       boolean NOT NULL DEFAULT false,
    position       integer NOT NULL DEFAULT 0,
    sprint_ref     text REFERENCES sprints(ref),      -- the sprint's identity since 2026-09-07
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
    date_moved    timestamptz,                       -- NULL when no move time was observed
    CHECK (board_key > 0 AND board_key < 2000000000),
    UNIQUE (project_id, task_number),
    -- The target the sprint's scoped cursor and decision keys need (§3.3, §3.8).
    -- Redundant with the primary key by design.
    UNIQUE (task_ref, sprint_ref)
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
    task_ref        text NOT NULL REFERENCES tasks(task_ref) ON DELETE CASCADE,
    -- The reference as the card writes it, always kept; and the same reference as a foreign key,
    -- set exactly when the board holds that card.  Split on 2026-09-07: see §8.6.
    depends_on      text NOT NULL,
    depends_on_task text REFERENCES tasks(task_ref),
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

`blocked_by` and `supersedes` are single-valued today. They are given their own tables rather than
columns because both are *relations between cards*: as columns they cannot carry a foreign key
without also forbidding a card that references one not yet imported, and `task_dependencies` is
where a later card adds a second dependency without a schema change. `task_supersessions` keeps its
one-row-per-card primary key so the current cardinality stays enforced.

The dependency's two columns are the 2026-09-07 import's second finding of a record with no
representable field: nine `blocked_by` values on this board name cards the board does not hold
(`triggered-agents-*`, `memory-mcp-*`), and a single foreign-keyed column would have dropped all
nine. `depends_on` keeps the reference verbatim and is half of the primary key, so the relation
survives; `depends_on_task` is the foreign key and carries the same reference whenever the card is
there, so a dependency that *can* be checked still is. "Unresolved" is then a query
(`depends_on_task IS NULL`) rather than an absence, and the check constraint forbids the two
columns from naming different cards. §8.6 states the choice and its reason.

`tasks.date_moved` is set on SQL create and every successful column move. Done retention reads
that stored episode time. Revision `0004` leaves historical rows NULL rather than inventing an
age; such rows remain visible as candidates with unknown time and are skipped until a real move.

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

CREATE TABLE issue_comments (                         -- added 2026-09-07; see below
    comment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    issue_id    text NOT NULL REFERENCES issues(issue_id) ON DELETE CASCADE,
    marker      text,                                 -- a role, or issue:* (§8.1)
    body        text NOT NULL,
    actor_role  text,
    actor_id    text,
    request_id  text UNIQUE,                          -- FK added in §3.13 step 2
    created_at  timestamptz NOT NULL
);
CREATE INDEX issue_comments_by_issue ON issue_comments (issue_id, created_at);

CREATE TABLE product_comments (                       -- added by 0004
    comment_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id  text NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    marker      text,
    body        text NOT NULL,
    actor_role  text,
    actor_id    text,
    request_id  text UNIQUE,
    created_at  timestamptz NOT NULL,
    product_ref text GENERATED ALWAYS AS ('product:' || product_id) STORED
);
CREATE INDEX product_comments_by_product ON product_comments (product_id, created_at);
```

**Four tables.** `issue_comments` exists because the 2026-09-07 import
found **479** comments on Issue rows and this section declared only two comment tables, both
foreign-keyed to their own entity — the largest single record loss the run reported. It is §3.7's
own shape with `issues` as the entity: the same columns, the same `UNIQUE (request_id)`, the same
claim key in §3.13 step 2, the same place in the order. The Product side was recounted on
2026-09-07 and had no existing comments, but the released board vocabulary permits a Product
comment. Revision `0004_product_issue_sql` adds the same entity/request-fenced shape before
PostgreSQL serves that vocabulary.

The marker becomes a column because today it is recovered by string-parsing the first line of the
comment body (`tasks._normalize_comment`), and a body whose first line happens to look like
`[something]` is indistinguishable from a real marker. `body` keeps the prose exactly as written,
without the `[marker]` prefix line; §8.1 says how the prefix is stripped once, at import.

### 3.8 Sprint decisions

```sql
CREATE TABLE sprint_decisions (
    decision_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sprint_ref    text NOT NULL REFERENCES sprints(ref) ON DELETE CASCADE,
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
    ON sprint_decisions (sprint_ref, issue_id) WHERE issue_id IS NOT NULL;
CREATE UNIQUE INDEX sprint_decisions_one_per_card
    ON sprint_decisions (sprint_ref, task_ref) WHERE task_ref IS NOT NULL;
```

Deferred constraints (they reference `tasks`, so they are applied in the last step of §3.13):

```sql
ALTER TABLE sprint_decisions
  ADD CONSTRAINT decided_issue_is_declared_by_this_sprint
      FOREIGN KEY (sprint_ref, issue_id)
      REFERENCES sprint_issues (sprint_ref, issue_id) MATCH SIMPLE,
  ADD CONSTRAINT decided_card_is_in_this_sprint
      FOREIGN KEY (task_ref, sprint_ref)
      REFERENCES tasks (task_ref, sprint_ref) MATCH SIMPLE;
```

Every check here is a rule `sprint_close.py` enforces in Python before a close writes anything: one
decision per ref, a verdict from the right vocabulary, a non-empty reason, and `actual` only on a
confirmation. Moving them into constraints is what stops a decision existing only in a staged
transaction file (§2.6 gap 2).

The two scoped foreign keys carry the sprint into the key for the same reason §3.3 does. They
target `sprint_issues (sprint_ref, issue_id)` — whose primary key that already is — and
`tasks (task_ref, sprint_ref)`, so an issue decision is only representable for an issue the
sprint *declared* and a card decision only for a card the sprint *holds*. This is exactly what
`plan_close_decisions` refuses in Python today: `"sprint close was given a decision for issue(s)
the sprint did not declare"`. `MATCH SIMPLE` is again the mechanism, and here it does the work of
the `decision_subject_is_exactly_one` check's other half: an issue decision has `task_ref IS NULL`
and so skips the `tasks` key, and a card decision has `issue_id IS NULL` and so skips the
`sprint_issues` key. Each row is checked by exactly the one key that applies to it.

### 3.9 Request ownership, events and idempotency

**One request-id namespace for the whole installation, and it is a table.**

Today `TaskAudit` owns request ids globally. `_refresh_committed_index` builds one
`_committed_offsets` map over the single `events.ndjson` journal
(`src/secretary/tasks.py:1505-1526`), `_pending_owner` resolves ownership under one
`.audit.lock` (`:1191-1269`), and `claim` additionally consults
`_product_issue_pending` so a Product/Issue staged intent cannot be shadowed by a generic record.
One id, one owner, whatever kind of operation claimed it — and reusing it with a different payload
is refused with `"request id belongs to another operation or payload"`.

Splitting uniqueness across `task_comments`, `sprint_comments` and `sprint_budget_events` would
narrow that namespace: `r` could be a task comment *and* a sprint comment *and* a budget charge.
`board_events` cannot close the gap, because an ordinary role comment is the generic `commented`
operation (`src/secretary/tasks.py:2156-2173`) and has no `EventKind`, so it never produces a
`board_events` row at all. A narrowed request-id namespace is a silent regression against DoD 5, so
the schema keeps the namespace whole:

```sql
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
```

Every other table that carries a `request_id` now references this one instead of owning its own
uniqueness. These are deferred constraints too — `task_comments` and the rest are created before
`requests` in reading order — and §3.13 places them. A child reaches `requests.ref` through the
column that already spells its entity's reference: since 2026-09-07 the three sprint-owned children
carry `sprint_ref` as their *scoping* column (§3.3), so none of them needs a generated column any
more. `issue_comments` is the one that does, because its scoping column is the Issue's bare id and
`requests.ref` spells an Issue `issue:<id>`; this fence runs **before** the keys below it:

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

The `UNIQUE` constraints those tables carried in their own definitions stay, and they now mean
something narrower and correct: at most one comment, and at most one budget charge, *per claimed
request*. `sprint_decisions.request_id` is deliberately **not** unique — one sprint-close request
legitimately produces many decision rows, all children of the one claim.

**Why the key is composite, and not `request_id` alone.** A plain
`FOREIGN KEY (request_id) REFERENCES requests(request_id)` proves only that *somebody* claimed the
id. It does not prove the child belongs to *that* claim, and running it proved the gap: with the
plain key, `INSERT INTO task_comments (task_ref, …, request_id) VALUES ('secretary-1580', …,
'req-sprint-1432-create')` was accepted — a card comment hanging off a `sprint.create` claim whose
`ref` is `sprint:1432`. Carrying the entity's own reference into the key refuses it, with the same
construction §3.3 uses for the sprint's cursors and §3.8 for its decision subjects. A sprint-owned
child reaches its reference through the scoping column it already carries; `issue_comments` reaches
its own through the one generated column added by the fence above this one.

What stays the writer's job, and is named here so nobody mistakes the key for it: comparing the
stored `operation` and `intent` on a conflicting claim. The database now enforces *one id, one
owner, one entity*; whether a retry carries the same payload is the step-1 comparison below, which
is exactly the division `TaskAudit` draws today between its journal index and
`_require_same_event`.

#### The request lifecycle, stated once

Two shapes of mutation exist, they have different lifecycles, and the previous revision of this
section conflated them — which is how it came to prescribe an `INSERT` its own `CHECK` rejects.
The rule is now explicit, and `request_settled_matches_status` is what enforces it.

**Shape A — the mutation is entirely inside the database.** This is almost every operation: card
transitions, comments, budget charges, sprint create, Product/Issue writes. It is one transaction
(§7.1), so it claims **directly as `committed`, with `settled_at` set in the same statement**:

For the delivered Product/Issue slice the five writer paths are Product create, Issue create,
Issue priority replace, Issue close and operator recovery of one of those operations. All enter the
one `SqlCardClient.transaction()` through the `ProductIssueStore._host_mutation` boundary. The host
may stage internally while building the
effect, but the stage is never externally visible: request claim, final entity and relationships,
comment where applicable, and `board_events` insert commit together. A failure after any one of
those statements rolls all of them back; retrying the same request performs one effect, while a
committed replay performs none. SQL therefore never returns `audit_pending` for such a rollback.

```sql
INSERT INTO requests (request_id, operation, intent, status, protocol,
                      entity_kind, ref, created_at, settled_at)
     VALUES (:rid, :operation, :intent, 'committed', :protocol,
             :entity_kind, :ref, now(), now())
ON CONFLICT (request_id) DO NOTHING
RETURNING request_id;
```

A shape-A operation **never writes a `staged` row**. If the transaction rolls back, the row is gone
with everything else it wrote; if it commits, the claim and its effects commit together. There is
no window between them, so there is nothing to recover and no partial state to interpret.

**Shape B — the mutation has a durable effect outside the database.** There are exactly two in this
product: writing a sprint's knowledge closeout through the knowledge writer (a git commit in the
private instance repo) and launching a head. These cannot join a database transaction, so they keep
the stage-then-settle contour the current `MutationEventTransaction` already uses, in **two**
transactions:

```sql
-- T1: claim the obligation.  This transaction COMMITS, so the staged row is durable.
INSERT INTO requests (request_id, operation, intent, status, protocol,
                      entity_kind, ref, created_at)
     VALUES (:rid, :operation, :intent, 'staged', :protocol,
             :entity_kind, :ref, now())
ON CONFLICT (request_id) DO NOTHING
RETURNING request_id;
COMMIT;

--   … the external effect happens here: the closeout is written, or the head is launched …

-- T2: settle it.
UPDATE requests SET status = 'committed', settled_at = now()
 WHERE request_id = :rid AND status = 'staged';
COMMIT;
```

**Can a staged row outlive its transaction? Yes — for shape B, and only for shape B.** That is the
entire point of it: a `staged` row that survives is the durable statement "an external effect was
promised and may have happened", which is exactly what `BoardEventPending` and the `recover_*`
entry points mean today. A shape-A operation cannot leave one, because its only transaction either
commits the whole thing or leaves nothing.

Those two `COMMIT;` lines are the transaction boundaries, not statements to paste into a `psql`
session: run under `psql`'s autocommit each statement is already its own transaction, and the
`COMMIT` then warns `there is no transaction in progress` while changing nothing. The boundaries
are what the driver opens and closes; the shape is two transactions with the external effect
between them.

**What writes a staged row:** T1 of a shape-B operation, and nothing else.
**What reads one:**

- the operation's own retry, which finds its claim, re-checks whether the external effect landed
  the way `recover_marker_comment` and friends do today, then runs T2;
- `export_board`'s refusal gate — `SELECT 1 FROM requests WHERE status = 'staged'` — which is the
  relational form of the existing "board export blocked by N unresolved pending audit record(s)"
  (§6.3);
- `secretary status`, for the operator.

`discarded` is the third status and belongs to shape B alone: an abandoned obligation an operator
settles deliberately, with `settled_at` set. A shape-A rollback is not a discard; it leaves no row
to discard.

**On conflict.** Zero rows returned means the id is already owned. The transaction reads the stored
row and compares `operation` and `intent` for equality:

- **different** → `"request id belongs to another operation or payload"`, the exact refusal
  `_require_same_event` gives today, and the transaction rolls back having written nothing;
- **same, `status = 'committed'`** → a replay: return the existing result and commit nothing new;
- **same, `status = 'staged'`** → necessarily a shape-B claim, because shape A leaves no staged
  row. Resume it: re-check the external effect, then run T2.

`operation` and `intent` are immutable once claimed. Only `status` and `settled_at` ever move, and
for a given row they move at most once. The one released exception is `TaskAudit`'s generic-stage
replacement (`_pending_owner`'s `replace_generic_pending`): a generic `stage` may replace a
*generic* pending record and never a protocol one. That is why `requests.protocol` exists, and
because only shape B has a staged row to replace, the rule now has a precise scope:

```sql
UPDATE requests SET intent = :intent
 WHERE request_id = :rid AND status = 'staged' AND NOT protocol;
```

**What this preserves, and what it does not change.** The caller contract is untouched: the same
`request_id` argument, the same "retry with the same request id" answer, the same refusal on reuse
with a different payload, the same global scope. `board_events.event_id` stays a primary key, which
is `TaskAudit.event_id_owner`'s global event-id namespace made relational the same way. What
disappears is the file lock that made all of this atomic outside a database.

This table is also the successor of `ProductIssueTransaction`'s staged intent documents — the role
the previous draft gave to a separate `command_requests` table. There is no `command_requests`: a
second table for staged intents would have re-created the split namespace this section exists to
prevent.

### 3.10 The seven `jsonb` columns, and why each is one

| Column | Why it is not relational |
|---|---|
| (J1) `sprints.observer` | An observer is a tagged union today (`{"kind":"head","profile":…}`) whose variants are owned by the head registry, which DoD 3 keeps out of this schema. A relational encoding would require modelling head kinds here. |
| (J2) `sprints.source_audit` | Provenance of a *restored* row: the three fields (`created_at`, `updated_at`, `board`) a previous installation's backend reported. It describes a foreign backend, so it must not become columns that later readers mistake for this store's own audit. |
| (J3) `tasks.extensions` | The open extension point that `tasks._normalize` already produces as `extensions.kanboard`. It is where an unrecognized metadata key survives import instead of being dropped (§8.2). It is deliberately not a place for any field this schema names. |
| (J4) `board_events.data` | Per-`EventKind` payloads: marker bodies, attempt usage in five token dimensions across three accounts, outcome dispositions. Twenty-plus kinds with disjoint payloads; one table per kind is a larger change than this sprint carries, and the payloads are already validated by `board/models.py` on the way in. |
| (J5) `requests.intent` | The frozen argument set of an arbitrary command, compared for equality on retry. Its shape is the command's, not the schema's. |
| (J6) `issues.extensions` | (J3) for an Issue, added 2026-09-07: the import found nine leftover metadata keys on 158 Issue rows and a lane that is not the product's, and an Issue had nowhere to keep them. Same rule, same §8.2 counting, same prohibition on holding a field this schema names. |
| (J7) `products.extensions` | The Product counterpart added by `0004`: metadata outside `record_type`, `product_id` and the relational project set remains lossless provenance and cannot override those known fields. |

Nothing else is JSON. In particular `product_projects`, `sprint_repositories`, `sprint_issues`,
`task_retry_heads`, `task_issues`, budget counters, resume fields and close decisions — all of
which are JSON strings in Kanboard metadata today — are relational above.

### 3.11 Not in this schema, deliberately

Agent runtime and head processes; the head registry (`heads/heads.yaml`, `heads/source.yaml`);
memory facts and the vector index; personas, adapters, policies; secrets and the secret store;
provider sessions, quotas and credentials; Orca bindings and automations; run journals
(`state/runs/**`); transcripts and artifacts; knowledge documents. DoD 3 forbids moving these in
this sprint, and none of them is a board entity.

### 3.12 Closed vocabularies: the rule, and every column it applies to

**The rule: a closed vocabulary is a `CHECK` constraint on the column. Never a reference table.**

The rule is uniform, and it is stated because the alternative is defensible and was rejected for a
reason. A reference table would let a vocabulary value be added by an `INSERT` rather than an
`ALTER TABLE`. But every one of these vocabularies is a `set` or a `StrEnum` compiled into the
product — `ISSUE_KINDS`, `_TASK_TYPES`, `_COMPLEXITIES`, `EventKind` — and the value is only ever
usable once the code that produces it ships. A reference table would create a second authority
that can drift from the enum: a row present in the database for a value no code emits, or a value
the code emits with no row. A `CHECK` cannot drift, because widening it *is* a numbered migration
(§7.4) and lands in the same migration the code change ships with.

Every column with a closed vocabulary, and its source of truth:

| Column | Values | Source |
|---|---|---|
| `products.state` | `active`, `archived` | `ProductState` |
| `issues.state` | `open`, `closed` | `IssueState` |
| `issues.issue_kind` | `bug`, `feature`, `question`, `improvement` | `product_issues.ISSUE_KINDS` |
| `issues.priority` | `P0`–`P3` | `product_issues.ISSUE_PRIORITIES` |
| `issues.close_reason` | `resolved`, `invalid`, `duplicate`, `wont_do` | `product_issues.ISSUE_CLOSE_REASONS` |
| `sprints.status` | `open`, `closed`, `stopped` | `SprintState` |
| `tasks.state` | the seven card states | `CardState` |
| `tasks.task_type` | `code`, `research`, or none | `tasks._TASK_TYPES` |
| `tasks.complexity` | `cheap`, `standard`, `hard`, `frontier` | `tasks._COMPLEXITIES` |
| `tasks.family_preference` | `auto`, `claude`, `codex` | `tasks._FAMILY_PREFERENCES` |
| `tasks.codex_launch_mode` | `tui` | `tasks._CODEX_LAUNCH_MODES` ← `head/command.py:CODEX_LAUNCH_MODES` |
| `sprint_budget_events.event_type` | six charged types + `infrastructure_blocked` | `sprints.BUDGET_RECORDED_EVENT_TYPES` |
| `sprint_decisions.verdict` | per subject kind | `sprint_close.ISSUE_VERDICTS`, `CARD_DISPOSITIONS` |
| `requests.status` | `staged`, `committed`, `discarded` | this schema |
| `board_events.kind` | the 23 `EventKind` values, listed in §3.9 | `board/models.py:EventKind` |
| `board_events.entity_kind`, `requests.entity_kind` | `product`, `issue`, `sprint`, `card` | `EntityKind` |
| `repositories.role` | `primary`, `curator_root` | this schema (§8.3) |

Three of these need a word about the import, because a `CHECK` that historical data cannot satisfy
would block the migration rather than protect it:

- **`tasks.task_type` admits NULL as well as its two values** since 2026-09-07. That is not a
  widening of the vocabulary and deliberately not the degenerate "any text": the two values are
  still the only values, and what was added is the *absence* of one. `secretary-583` carries no
  `task_type` metadata at all, `tasks._card` reads it as `''`, and a `NOT NULL` column made that
  card the one record of 944 the store could not hold (§8.6). `task_type IS NULL OR task_type IN
  ('code','research')` spells both halves out rather than relying on `NULL IN (…)` evaluating to
  unknown, so a reader of the constraint sees the decision instead of inferring it from
  three-valued logic. The importer stores NULL only where the board says nothing; a value outside
  the vocabulary is still refused and named, exactly as before.

- **`tasks.codex_launch_mode` is a one-value vocabulary today**, and `tasks.py` normalizes a
  *retired* launch mode away on read (`_enum_or_none(meta.get("codex_launch_mode"),
  _CODEX_LAUNCH_MODES)`), so a historical row carrying a retired mode already reads as `None`.
  The importer stores `NULL` for those rows, exactly as the current reader reports them, and the
  `CHECK` therefore rejects nothing that exists. The retired spelling is not silently discarded:
  it stays in `tasks.extensions` (§8.2) with the raw metadata, so the fact that the row once named
  another mode is still queryable.
- **`board_events.kind` is checked against the 23 values `EventKind` declares.** The audit journal
  is append-only and its historical records were written by earlier versions of this same enum, so
  a record whose kind is not in the list is a record the *current* product cannot parse either —
  `board/analytics.py` and `_event_action` already treat it as unrecognized. The importer reports
  any such row rather than importing it under a value the schema does not know, and §8.5's rule
  applies: a record that cannot be represented is named in the report, never dropped in silence.

`tasks.claim_worker`, `tasks.slug`, `tasks.base_branch`, `sprints.worker_pin`, `task_comments.marker`
and the head/profile columns are deliberately **not** in this table. They are open vocabularies:
their values come from the head registry or from an operator, and the head registry is out of this
schema by DoD 3 (§3.11). Constraining them here would put a second copy of the registry in the
database, which is the drift this section's rule exists to avoid.

### 3.13 Executable order

The DDL above is grouped by entity so it can be read. That is **not** the order it executes in, and
saying "the document's order" without saying which order is how a reader ends up running
`ALTER TABLE sprints … REFERENCES tasks` before `tasks` exists. So the order is stated here, and it
is the order migration `0001` applies:

1. **Create tables**, in §3 reading order: §3.1 (`products`, `projects`, `repositories`,
   `product_projects`), §3.2 (`issues`), §3.3 (`sprints`, `sprint_repositories`, `sprint_issues`,
   `sprint_resumes`, `sprint_number_seq`), §3.4 (`sprint_budget_events`), §3.5 (`tasks`,
   `task_retry_heads`, `task_issues`, `task_dependencies`, `task_supersessions`), §3.6
   (`sprint_projects` and its partial unique index), §3.7 (`sprint_comments`, `task_comments`,
   `issue_comments` and their indexes), §3.8 (`sprint_decisions` and its two partial unique
   indexes), §3.9 (`requests`, `board_events`),
   §7.4 (Alembic's `alembic_version`, which the migration tool creates).
2. **Add the deferred constraints**, in the same reading order: §3.3's two scoped sprint cursors,
   §3.4's `budget_card_is_in_this_sprint`, §3.8's two scoped decision subjects, §3.9's generated
   `issue_ref` column and its six `request_id` foreign keys.

`issue_comments` takes §3.7's place in both steps, because it is §3.7's table: it is created with
the other two, and its `issue_comment_claims_its_request` is added with the other claim keys. That
is the whole of its placement, and it is why the order above did not otherwise change on
2026-09-07.

Every fence in §3 that begins `ALTER TABLE` is a step-2 fence and is labelled as one. Every fence
that begins `CREATE` is a step-1 fence. Nothing else needs to be decided at execution time.

**The catalogue, per revision.** The schema of §3 is built by seven Alembic revisions, and a card
that checks its work against a migrated database needs the numbers of the one it ran. Both are
counted from a real `postgres:16` by `tests/test_board_store_schema.py`, never asserted from
reading:

| After | Tables | `CHECK` | Foreign keys | Primary keys | `UNIQUE` | Partial unique indexes |
|---|---|---|---|---|---|---|
| `0001_initial` (the numbers §10's run produced) | 22 | 34 | 36 | 22 | 12 | 4 |
| `0002_board_gaps` (2026-09-07, the gaps the first import of real data found) | 23 | 37 | 38 | 23 | 13 | 4 |
| `0003_task_type_optional` (2026-09-07, the last card that import could not write) | 23 | 37 | 38 | 23 | 13 | 4 |
| `0004_product_issue_sql` (2026-09-08, Product/Issue and Done retention) | 24 | 37 | 40 | 24 | 16 | 4 |
| `0005_sprint_sql` (2026-09-08, Sprint runtime and ordered evidence) | 24 | 37 | 40 | 24 | 15 | 4 |
| `0006_sprint_transport_key` (2026-09-08, disjoint indexed Sprint transport keys) | 24 | 38 | 40 | 24 | 16 | 4 |
| `0007_card_transport_key` (2026-09-09, collision-free indexed Card transport keys) | 24 | 39 | 40 | 24 | 17 | 4 |

The last table and the last primary key are Alembic's `alembic_version` in both rows. The deltas
are the whole of `0002`: one table (`issue_comments`) with its primary key, its `UNIQUE
(request_id)` and its foreign key to `issues`; the claim key
`issue_comment_claims_its_request`; `task_dependencies` trading its foreign key on `depends_on` for
one on `depends_on_task`; the sprint's identity moving from `sprint_number` to `ref`, which trades
`sprints`' `UNIQUE (ref)` for a `UNIQUE (sprint_number)` and adds the two `CHECK`s that keep a
numbered reference and its number spelling the same thing; and
`dependency_resolution_is_the_same_reference`, the third new `CHECK`.

`0003`'s row repeats `0002`'s six numbers because every one of them is unchanged, and it is
recounted from the same container rather than assumed: the revision drops one `CHECK` on
`tasks.task_type` and creates one in its place, and makes a column nullable, which no count here
measures. The number that *is* different is the one this table does not carry — the column's
`NOT NULL`, which is what the revision is for.

`0004` adds `product_comments`, its entity and request foreign keys, primary key and request
uniqueness; indexed unique `board_key` columns on Products and Issues; Product extensions; and
nullable `tasks.date_moved`. The timestamp deliberately stays NULL for history whose move time
the SQL store never observed.

`0007` upgrades an occupied `0006` store in place. It assigns every existing task a distinct Card
key in stable reference order, advances `card_board_key_seq` beyond the backfill, settles the deferred
foreign-key checks the rewritten rows queued (`SET CONSTRAINTS ALL IMMEDIATE`), then makes the
column non-null, unique and range-checked. It does not rewrite refs, per-project numbers, ownership,
archive state, relations, comments or audit records.

The split exists because four of the schema's relations are forward or mutual, and a scoped
foreign key (§3.3) makes that unavoidable rather than incidental: `sprints` must exist before
`tasks` can reference it, and `tasks` must exist before `sprints`' cursor can be scoped to it.
Splitting create from constrain is the ordinary answer and costs one extra step.

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
 WHERE sprint_ref = 'sprint:1432' AND reserved;
```

After it, `SELECT * FROM sprint_projects WHERE sprint_ref = 'sprint:1432'` still returns both rows,
with `reserved = false` and a `released_at`. That is the history the DoD asks to keep, and
`reserved_matches_release` makes "released but still marked reserved" unrepresentable.

The scoping column is the sprint's reference, not its number: `0002_board_gaps` made
`sprints.ref` the primary key (§3.3, §9), so every table that names a sprint names it the way the
board spells it. The statement above is executed as written by §10's step 7, which is how this
paragraph stopped being a statement about a column that no longer exists.

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

**Implemented: `postgres:16` in its own Docker container, managed by
`/opt/secretary/postgres-compose.yml`, which the product writes and verifies.** Not a system
package. Bootstrap creates it; every configured upgrade reconciles it before Alembic runs.

The reasons, from the inventory and the host:

- The host has Docker (`/usr/bin/docker`), `postgres:16` is already pulled (the codegen stack's
  `…-db-1` runs it), and `bootstrap.py` already owns this pattern: it writes
  `/opt/secretary/kanboard-compose.yml` (mode 0600) and runs `docker compose up --detach` with
  `--env-file <instance>/board-transport.env`. A second service in that shape adds no new
  installation mechanism, no new failure mode in `bootstrap.py`, and no new privileged step.
- The host has **no `psql` and no `pg_dump`** (`which psql pg_dump` finds nothing). A system package
  would install a server *and* the client tools; a container installs the server and puts the
  client tools inside it, reachable as `docker exec secretary-postgres-1 psql …`. Either works, and
  the container keeps the client tools *version-matched to the server* without adding an apt
  repository pin to `bootstrap.py`. What a later card does with those client tools for backup is
  §5.7's open question, not a claim made here.
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
      POSTGRES_USER: secretary_owner            # the owner role IS the init role; see §5.5
      POSTGRES_PASSWORD: ${SECRETARY_DB_OWNER_PASSWORD}
    volumes:
      - board-db:/var/lib/postgresql/data
volumes:
  board-db:
```

A named volume rather than a bind mount under `<data>`: `secretary-data` is the derived plane that
`data.py:init_layout` creates and `backup.py` copies wholesale, and a live PostgreSQL data
directory inside it would be copied mid-write by a file-level backup. Keeping it out of `<data>`
makes "the database is not a file the checkpoint snapshots" a property of the layout, not a rule
somebody has to remember. That holds whichever way §5.7's open question is answered: nothing the
file-level backup copies is a live PostgreSQL data file.

The Compose project is `secretary-board-store`, so the durable volume is
`secretary-board-store_board-db`. Reconciliation verifies the exact image, restart policy,
loopback publication and mount before `compose up`; drift is refused rather than silently
recreating the container. An existing volume with no `board-store.env` is also refused: the image
environment initializes only an empty volume and is not authority for an existing owner's
password.

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

It carries **one credential per role**, not one credential. A single `SECRETARY_DB_USER` would
make §5.5's three-role boundary documented and unreachable: every consumer, including the ones
assigned `secretary_read`, would connect as the owner.

```
SECRETARY_DB_HOST=127.0.0.1
SECRETARY_DB_PORT=5432
SECRETARY_DB_NAME=secretary
SECRETARY_DB_OWNER_USER=secretary_owner
SECRETARY_DB_OWNER_PASSWORD=<generated at bootstrap>
SECRETARY_DB_APP_USER=secretary_app
SECRETARY_DB_APP_PASSWORD=<generated at bootstrap>
SECRETARY_DB_READ_USER=secretary_read
SECRETARY_DB_READ_PASSWORD=<generated at bootstrap>
```

Nine keys, all required, parsed with `board_transport.parse`'s all-or-nothing rule: an unknown key,
a missing key, an empty value, a symlink or `mode & 0o077` each refuse the file rather than
producing a partial configuration. The three passwords are independently generated at bootstrap
with Python's `secrets` primitive; the user names are fixed, so only the passwords vary between
installations. The ignore is written before the credentials, and the complete mode-0600 file is
published atomically. Reconcile never replaces it. A missing file is an explicit no-op for an
installation that has not been provisioned; a symlink, broad mode, unknown/missing/empty key or
tracked file stops upgrade before container or migration work.

**Why not the secret store.** `secret_store.py` exists for values whose loss makes an installation
unrecoverable and which must survive a rebuild on a clean host from a recovery phrase. The database
password is not one: it is regenerable by recreating the role, it is meaningless without the volume
it guards, and it is needed by `docker compose up` *before* the instance repository is necessarily
in a state where the store can be opened. Kanboard's API token was excluded from the store for
exactly this reason and this follows it. What *does* belong in the secret store, if and when it
exists, is an off-host replication or backup credential — the same distinction
`github.checkpoint-token` already draws.

**How each consumer gets it.** One resolver with a role argument, the same shape
`KanboardClient.for_instance` has today:

```python
board_store.resolve(instance_dir, role)     # role: "owner" | "app" | "read"
```

The caller does not choose the role freely. **The construction path binds it**, which is what makes
the boundary reachable rather than advisory:

| Constructor | Role | Why |
|---|---|---|
| `BoardStore.owner_for_instance(instance)` | `owner` | Called from exactly two places: the migration runner in `upgrade.py` (§7.4) and `bootstrap.py`. It is not reachable from `tasks.py`, `sprints.py`, `product_issues.py` or `webproto/*` at all. |
| `BoardStore.writer_for_instance(instance)` | `app` | Every path that constructs a writer: `TaskWriter`, `SprintWriter`, `ProductIssueStore`, the dispatcher runtime, `webproto/ops.py`, `webproto/sprint_ops.py`, `webproto/admission.py`. |
| `BoardStore.reader_for_instance(instance)` | `read` | Every read-only construction: a standalone `TaskReader`/`SprintReader`, `status.py`, `webproto/reads.py`, `webproto/sprint_reads.py`, `webproto/pause_reads.py`, `data.py:export_board`. |

This is reachable because `TaskReader` and `TaskWriter` are already separate classes taking a
client, so the distinction exists in the call graph today; the connection role simply follows the
one already there.

**What the boundary is, stated exactly.** It is **per process, not per statement.** A process that
both reads and writes — the dispatcher tick, `webproto/ops.py`, any CLI write verb — takes one
`app` connection and does its reads on it. A `TaskReader` constructed *by* a writer path shares
that writer's connection and is therefore an `app` read. What `secretary_read` buys is that a
process which only reads *cannot* write even if a defect asked it to: the web transport's read
paths, `status.py` and the export run under a role with no `INSERT`. Claiming more than that would
be claiming a boundary the code shape cannot hold, and §5.5's table now says which construction
path each role belongs to rather than which module, because a module can contain both.

- CLI (`task_commands.py`, `sprint_commands.py`, `product_issue_commands.py`): `--instance` /
  `SECRETARY_INSTANCE` per invocation; `app` for a write verb, `read` for a pure read verb.
- web (`secretary-web.service`): `SECRETARY_INSTANCE`, already in the unit. Its read layers resolve
  `read`, its operation layers resolve `app`; both live in one process and hold separate pools.
- dispatcher (`secretary-dispatcher-production.service`): `SECRETARY_INSTANCE`, already in the
  unit, plus its `EnvironmentFile=-<instance>/runtime.env`. Always `app`.
- observer, worker, reviewer heads: they run the CLI, and `role_env`/`runtime_env` already
  propagate `SECRETARY_INSTANCE` and `SECRETARY_DATA_DIR` into every head process. No new
  propagation. Role follows the verb, as for any CLI caller.

### 5.5 Roles and privileges

Three roles, so that a read-only consumer cannot write and the migration runner is not the
application:

| Role | Resolved by | Privileges |
|---|---|---|
| `secretary_owner` | `BoardStore.owner_for_instance` — the migration runner (§7.4) and `bootstrap.py`, nothing else | owns the schema; `CREATE`, `ALTER`, `DROP`, and `CREATE ROLE` for the two below |
| `secretary_app` | `BoardStore.writer_for_instance` — every writer construction, and the reads those writers make on the same connection | `SELECT, INSERT, UPDATE, DELETE` on all tables, `USAGE` on sequences; **no** DDL |
| `secretary_read` | `BoardStore.reader_for_instance` — every read-only construction | `SELECT` only; `USAGE` on the schema |

`DELETE` is granted to `secretary_app` because the current writers genuinely delete: `removeTask`
compensates a failed sprint create, and `ON DELETE CASCADE` on the link tables is how a compensated
create leaves nothing behind. Nothing in the current product deletes a card or a closed sprint —
archival is `archived = true`, and §4 releases a reservation by `UPDATE`.

**Where the owner identity comes from.** The compose service initializes the cluster with
`POSTGRES_USER=secretary_owner`, so the owner role *is* the initialization role and no mapping is
needed — the previous draft's `POSTGRES_USER=secretary` plus a note that it "is" the owner was the
gap. The official `postgres:16` entrypoint creates the role named by `POSTGRES_USER` as a
superuser owning `POSTGRES_DB`, and it does so **only on an empty data directory**: on every later
start the authority is the role in the volume, not the environment variable.

**Where the other two come from.** Migration `0001`, run as `secretary_owner`, creates them and
grants their privileges. The runner supplies the two generated passwords from `board-store.env`;
they are not literals in the migration file, and the file is therefore identical on every
installation, which is what lets §7.4's checksum rule work at all. PostgreSQL does not accept a
bound parameter in `CREATE ROLE`, so the runner composes the statement with `psycopg.sql.Identifier`
and `psycopg.sql.Literal` rather than string formatting:

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

The `ALTER DEFAULT PRIVILEGES` statements matter as much as the grants: without them, a table
created by migration `0007` is invisible to `secretary_app` until somebody remembers to grant it,
which is a failure that appears long after the migration that caused it.

**Rotation.** Same shape for all three, and it is a reconcile, not a new subsystem:

1. generate a new password;
2. `ALTER ROLE <role> PASSWORD <new>` on an owner connection;
3. rewrite `board-store.env` whole and by rename, the way `secret_store.materialize_secrets` writes
   `runtime.env`, so no reader ever sees the file half-written;
4. restart the long-lived consumers (`secretary-web.service`,
   `secretary-dispatcher-production.service`). Short-lived CLI processes pick the new value up on
   their next invocation with no restart at all.

The owner's own password is the one asymmetry, and it is a property of the container rather than of
this design: `POSTGRES_PASSWORD` is read only when the entrypoint initializes an empty volume, so
after the first start rotating it means step 2 and nothing else — editing the compose environment
alone would change nothing and would leave the file disagreeing with the cluster. That is worth
stating because the equivalent Kanboard reconciliation *does* flow through `--env-file`, and the
two are not the same.

The current CLI deliberately exposes no implicit rotation flag. Until a dedicated operator
rotation command performs the four steps above as one controlled operation, edit neither the file
nor the Compose environment: ordinary bootstrap and upgrade preserve the working credentials and
refuse mismatches.

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
and costs memory); per-process pooling of **one connection for a short-lived CLI process** (it runs
one command under one role) and, in the web process, **two pools rather than one** — max 8 on
`secretary_read` for the read layers and max 4 on `secretary_app` for the operation layers, because
§5.4 binds the role to the construction path and a shared pool would erase that. The dispatcher
holds one `app` connection per tick. Even with both web pools full the total stays under 20, well
inside the 50. No PgBouncer: at this connection count it is a component to operate for no measured
benefit, and the measurement that would justify it does not exist yet.

Board size for sizing: 1482 Pipeline cards, open and archived, on 2026-09-06
(`state/knowledge/measurements/2026-09-06-sprint-form-catalogue-n-plus-one.md`). Every table in §3
is small; the /sprints/new budget the same measurement fixed (≤ 3 s, and 8 batched metadata reads
instead of 3000) becomes one indexed join.

### 5.7 Backup and restore of PostgreSQL — open, and deliberately not decided here

**This document does not design the backup and restore of the PostgreSQL store.** Not the artefact
format, not where a dump sits in an archive, not the restore order, not how roles come back on a
clean host. A later card in `sprint:1432` owns it, and owns it *together with the code it needs*.

What is settled here is only why it cannot be settled here, as four facts about the existing
chain. They are written down so nobody re-derives them, and so the later card starts from them
rather than from a first reading of `backup_policy.py`:

1. **A dump written under `<data>/backups/` never enters a `full` archive.**
   `backup_policy.should_skip_data_entry` returns `True` for any relative path whose first part is
   `backups` (`src/secretary/backup_policy.py:194-195`), before any component or policy check. So
   the obvious placement — write `board-db.dump` beside the archives — produces a dump that no
   archive carries.
2. **A custom-format dump does not pass verification in the `raw_board` slot.** In `FULL_POLICY`
   the `raw_board` component is the directory `board` with `requires_raw_board_data=True`
   (`backup_policy.py:90-98`), and `_verify_raw_board_component` requires a `manifest.json` under
   that component *and* at least one regular file under `data/**`
   (`backup_verify.py:235-247`). A single `pg_dump -Fc` file satisfies neither, so reusing the slot
   the Kanboard `docker cp` dump occupies fails `secretary backup verify`.
3. **The roles are not in the dump.** PostgreSQL roles are cluster-global objects; `pg_dump` of a
   single database does not contain them. After a restore onto a fresh cluster,
   `secretary_app` and `secretary_read` therefore do not exist — while the restored
   `schema_migrations` row already asserts that the migration which creates them has run
   (§5.5, §7.4). Restore leaves the database claiming a state its cluster does not have.
4. **After cutover, backup does not run at all until that code changes.** `backup.py:117` calls
   `raw_kanboard_dump(data_dir)` unconditionally, before the loop over the requested kinds, so it
   runs for a `core`-only backup exactly as it does for a `full` one. That function shells
   `docker cp` from the hard-coded Kanboard container with `check=True` and turns any failure into
   a `RuntimeError` (`data.py:116-160`). Separately, `FULL_POLICY` requires the `raw_board`
   component (`backup_policy.py:90-98`), so `full` cannot simply stop producing the artefact
   either. The consequence is concrete: with the Kanboard container gone, `secretary backup` fails
   outright; with it left running, a `full` archive still carries a raw Kanboard dump that says
   nothing about the new store. Post-cutover backup is therefore **broken until the later card
   changes this code**, not merely unmodernized.

**The conclusion this document is obliged to state.** Each of the four is a property of code, not
of prose. Carrying a PostgreSQL dump in the existing archive requires changing
`backup.py`, `backup_policy.py` and `backup_verify.py` — a new component with its own policy and
its own verifier, or an exemption in `should_skip_data_entry`, plus a role-bootstrap step outside
the dump; and fact 4 says the unconditional raw dump has to go in the same change, or nothing
backs up at all. That is a code change, so it belongs to a card that ships the code and the test,
not to a design document. Anything this document decided about it would be decided without the one
check that has actually caught defects here: executing it. Fact 4 also fixes the ordering: that
card lands **before or with** cutover, not after it.

**What is unchanged and needs no card.** The normalized export stays the recovery canon. It is
generated from PostgreSQL instead of from Kanboard; `export_board` swaps its reader while the file
contract (`cards.ndjson` / `sprints.ndjson` / `events.ndjson` / `export.json` and the seal), the
validation and the pathspecs stay as they are. `docs/RECOVERY.md`'s "What the checkpoint contains"
and "Source of truth" stay true word for word — only the identity of the live backend changes — and
`board/analytics.py` keeps working on a copied `state/board` directory. A `core` archive keeps
carrying exactly that normalized, engine-independent set, and `restore_capability`
(`normalized-core`, `full-snapshot`) keeps its meanings. The open question is the engine-specific
artefact, not the portable one. Read that as a statement about what an archive *holds*, not as a
claim that the command *runs*: fact 4 is why nothing is archived at all until the later card
changes `backup.py`.

**The open question, for the later card.** Does the `full` archive carry a PostgreSQL dump at all —
and if it does, in which component, verified by what, with roles restored how, and what replaces the
unconditional `raw_kanboard_dump` call fact 4 names? This document deliberately makes no prediction
about what an archive contains in the window before that card lands, because fact 4 says there is no
such window in which backup works.

Nothing else in this document depends on the answer: §5.1 puts the client tools in the container and
§5.2 keeps the data directory out of `<data>` so that a file-level backup cannot copy it mid-write,
and both hold whichever way the question is decided.

### 5.8 The Python driver as a new core dependency

**`psycopg[binary] >= 3.2`**, added to `[project].dependencies` in `pyproject.toml` — and, since
the owner's decision of 2026-09-07 (§7.4), **`SQLAlchemy >= 2.0`** and **`alembic >= 1.13`**
beside it. The driver argument below is unchanged and applies to all three: the schema an
installation cannot build or verify is a board it cannot serve. SQLAlchemy speaks to PostgreSQL
*through* `psycopg`, so the driver choice is not superseded by them.

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
  `secretary upgrade` installs all three. `bootstrap.py` gets them on a fresh install through the
  same venv install.
- **Offline installation.** This is a real regression in the offline story and is named rather than
  papered over. Today the three original core dependencies (PyYAML, jsonschema, cryptography) are
  already PyPI wheels, so `pip install -e .` on a host with no index already fails; three more
  distributions do not change the *kind* of failure. What they do change is that the failure now
  happens on a host that previously might have had the three cached, and that the staging list is
  six names rather than four. Two consequences the delivery card must handle:
  (1) `psycopg[binary]` must be present in the venv **before** the first migration runs, so the
  upgrade step ordering is dependencies → migrations → services; (2) an air-gapped install needs
  the wheels staged the same way the other three are, and `secretary upgrade` must report
  `dependencies: failed` loudly rather than proceeding to a migration it cannot run. No new
  offline mechanism is introduced by this card.
- **Version pin.** A floor (`>= 3.2`, `>= 2.0`, `>= 1.13`), not an equality pin, matching how
  PyYAML, jsonschema and cryptography are declared. Ruff is the only equality-pinned dependency,
  because a linter's version is part of a gate's meaning; a driver's and a migration tool's are
  not.

---

## 6. Ownership boundaries

After cutover, for each thing: what is canonical, and who writes it.

### 6.1 Canonical in PostgreSQL

Which of the two implementations serves cards is not a property of the data and is therefore not
in this table: it is `SECRETARY_CARD_BACKEND` (§2.2), one value per process, `kanboard` until an
operator says otherwise.  The rows below describe the store once cards are served from it; the
switch is what makes reaching that state reversible, since returning the value to `kanboard`
restores today's behaviour with no data migration in either direction.

For the same reason the identity a normalized row carries — `<kind>_<backend>_<n>` (§2.2) — is not
canonical either, and is not a column here.  It names which implementation answered *this* read;
it is not the row's identity, which is the reference (§9).  A record written while Kanboard served
the cards keeps `task_kanboard_<n>` for ever, and after a cutover the same card reads as
`task_postgres_<n>`; both are read by the same function, so no consumer has to know which era its
input came from.


| Data | Writer after cutover |
|---|---|
| products, issues, sprints, tasks | `secretary_app`, through the board protocol: dispatcher tick, CLI commands, `webproto/ops.py` and `webproto/sprint_ops.py` |
| `product_projects`, `sprint_projects`, `sprint_repositories`, `sprint_issues`, `task_issues` | same |
| `projects`, `repositories` — **derived, not canonical** | `board_store.sync_project_registry`, projecting `<instance>/projects/*.yaml` inside the referencing transaction and again on `reconcile apply` / `upgrade` (§3.1). The files stay canonical (§6.2); a removed binding sets `registry_present = false` and is never deleted |
| `task_dependencies`, `task_supersessions` | same |
| `sprint_comments`, `task_comments` | same |
| `sprint_decisions`, sprint close reason and closeout document *path* | `SprintWriter.close` |
| `sprint_budget_events` | `SprintWriter.record_budget`, dispatcher |
| `sprint_resumes` | `SprintWriter.resume`, observer through the CLI |
| `requests`, `board_events` | the protocol seam (`BoardEventCanon`, `MutationEventTransaction`) and, for cards, `board/sql_audit.py`, which is `TaskAudit`'s contract over these two tables (§7.3); every writer claims a `requests` row before any other write |
| card state, archive, claim, routing | dispatcher and CLI writers, through `TaskWriter` over `board/sql_cards.py` |

### 6.2 Canonical in files and git snapshots

| Data | Location | Writer after cutover | Changed? |
|---|---|---|---|
| project/repository bindings | `<instance>/projects/*.yaml` | the operator, by hand | no — and `registered_projects()` keeps reading the files, so admission is unchanged. The `projects`/`repositories` tables are a derived projection of these files with a named writer (§3.1, §6.1), not a second canon |
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
   `requests WHERE status = 'staged'` — the same refusal, cheaper.
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
BEGIN;                                            -- READ COMMITTED, see §7.2
  SET CONSTRAINTS ALL DEFERRED;                   -- the sprint and its cursors land together
  -- 1. claim the id in the one global namespace (§3.9).  Sprint create is shape A -- entirely
  --    inside the database -- so it claims as 'committed' with settled_at in the same statement.
  --    Zero rows means it is already owned: read the row, compare operation and intent, and
  --    either refuse or return the prior result.
  INSERT INTO requests (request_id, operation, intent, status, protocol,
                        entity_kind, ref, created_at, settled_at)
       VALUES (:'rid', 'sprint.create', :'intent'::jsonb, 'committed', true,
               'sprint', :'ref', now(), now())
  ON CONFLICT (request_id) DO NOTHING
  RETURNING request_id;
  -- 2. project the registry rows this create is about to reference (§3.1), then the entity
  --    and every link that is part of the same fact.
  INSERT INTO projects (project_id, enabled, registry_present)
       VALUES ('secretary', true, true), ('secretary-instance', true, true)
  ON CONFLICT (project_id) DO UPDATE
      SET enabled = EXCLUDED.enabled, registry_present = true;

  INSERT INTO sprints (ref, sprint_number, goal, definition_of_done, product_id,
                       status, observer, created_at, updated_at)
       VALUES ('sprint:1432', 1432, :'goal', :'dod', 'secretary', 'open',
               '{"kind":"head","profile":"claude-observer-medium"}'::jsonb, now(), now());

  INSERT INTO sprint_projects (sprint_ref, project_id, reserved, reserved_at)
       SELECT 'sprint:1432', project_id, true, now()
         FROM projects
        WHERE project_id IN ('secretary', 'secretary-instance');   -- may raise on §4's index

  INSERT INTO sprint_issues (sprint_ref, issue_id)
       SELECT 'sprint:1432', issue_id FROM issues WHERE issue_id = '7ebdf89a53c541a8d44b';

  INSERT INTO sprint_repositories (sprint_ref, repository_id)
       SELECT 'sprint:1432', repository_id FROM repositories
        WHERE path IN ('/home/dev/secretary', '/home/dev/secretary-instance');

  -- 3. the event, whose (request_id, ref) pair claims the request of step 1
  INSERT INTO board_events (event_id, request_id, kind, entity_kind, ref, actor_role, actor_id,
                            reason, target_state, occurred_at, committed, committed_at)
       VALUES (:'eid', :'rid', 'entity.created', 'sprint', :'ref', 'po', :'actor',
               'sprint opened', 'open', now(), true, now());
COMMIT;
```

The statements are written out rather than elided as `(...)`, because an example with placeholders
cannot be executed and an example nobody executes is how the two defects of the previous revision
reached a reviewer. This one is run as written (§10); the only substitutions are the `:'name'`
bind parameters.

The sprint is inserted with both `ref` and `sprint_number`, and the four links that follow name the
reference: `0002_board_gaps` made `sprints.ref` the primary key (§3.3, §9) and moved every scoped
key onto it. `sprint_number` is still written — `sprint:1432` is a numbered reference and
`sprint_number_agrees_with_ref` requires the number of a numbered one — and `sprint_number_seq` is
still what allocated the 1432. This is a third defect of exactly the class §10 exists to catch: the
transaction above was executable against `0001` and unexecutable against `0002`, and running it is
what said so.

Step 1 is not decoration and it is not specific to sprint create: **every** mutation opens with it,
including the ones that write no `board_events` row at all — an ordinary role comment, a budget
charge. That is what keeps the request-id namespace whole (§3.9), and it is why the claim is a
separate first statement rather than a column on whichever table the operation happens to touch.

`settled_at` is set in the same statement because `request_settled_matches_status` requires it:
`staged` means "settled_at IS NULL" and nothing else. A shape-A mutation is never staged (§3.9), so
omitting `settled_at` here would make this transaction unexecutable — which is precisely what the
previous revision of this document prescribed, and what running it against a real `postgres:16`
caught.

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
| `TaskAudit`'s single global request-id index over `events.ndjson` (`_refresh_committed_index`, `_pending_owner`), plus its `_product_issue_pending` cross-check | one `requests` table, one primary key, one namespace for the whole installation (§3.9). Comments and budget charges claim it too, so nothing falls outside it the way the generic `commented` operation would fall outside `EventKind` |
| `TaskAudit` committed/pending records in `events.ndjson` + `pending-audit/v2-<sha256>.json`, indexed under a file lock | `requests.status` (`staged`/`committed`/`discarded`) and `board_events.committed` |
| `BoardEventCanon.stage` / `commit` / `committed(request_id)` | `INSERT INTO requests … ON CONFLICT (request_id) DO NOTHING`, then compare the stored `operation` and `intent` |
| `_pending_owner`'s `replace_generic_pending` (a generic stage may replace a generic pending record, never a protocol one) | the single permitted `UPDATE requests SET intent = … WHERE status = 'staged' AND NOT protocol` (§3.9); `requests.protocol` is what makes "never a protocol one" checkable |
| `TaskAudit.event_id_owner` — which request id already published an event id | `board_events.event_id` PRIMARY KEY, globally unique |
| `_require_same_event`: "request id belongs to another operation or payload" | the same comparison against the stored `requests.operation` / `requests.intent`, for typed and generic operations alike |
| `MutationEventTransaction`'s stage → effect → confirm → finish → commit | one transaction; `confirm` disappears, because a committed transaction *is* the confirmation |
| `BoardEventPending` and the four `recover_*` entry points, for a mutation entirely inside the board | **gone**: a shape-A transaction that does not commit leaves nothing, so there is nothing to recover |
| The same, for the two effects outside the board (knowledge closeout, head launch) | **kept**, as shape B: a durable `requests` row with `status = 'staged'`, settled by its own second transaction after the effect is re-checked (§3.9) |
| `ProductIssueTransaction` staged intent documents | `requests` rows with `status = 'staged'` |
| `marker_comment_lock` (per-card flock, because marker prose carries no request id) | `task_comments.request_id` unique **and** referencing `requests`; markers get a request id at the seam |

The contracts callers see are unchanged: the same `request_id`, the same "retry with the same
request id" answer, the same refusal when a request id is reused with a different payload, and the
same *scope* for that refusal — one id, one owner, whatever kind of operation claimed it. What
disappears is the class of failure where the effect landed and the record did not — the reason
`recover_*` exists at all.

That sentence includes the Card state edge, and it is worth naming the three states it removes,
because they are the ones the Kanboard journal has recovery paths for. A transition is
`TaskWriter._transition_card`, and it runs inside `TaskWriter._mutation()`: on PostgreSQL the
staged `requests` row, the `moveTaskPosition` that changes `tasks.state`, the caller's own board
work for the same edge (`finish` — the claim's metadata write, the Ready reset, the reason
comment) and the committed `board_events` row are statements of one transaction. So on this
backend there is no In progress card whose claim metadata was never written, no Ready card whose
routing was never reset, and no moved card beside a `requests` row still `staged`: a failure at
any of those points rolls the column effect back together with the record, and the caller is told
the mutation did not happen rather than that a repair is owed. `recover_transition` and the
pending typed record it settles therefore have nothing to do here, which is what the
`BoardEventPending` row of the table above already says. Done retention closes a card the same
way and therefore stands on the same boundary: `TaskWriter.retire_done` runs its freshness guard,
its `closeTask`, the proof of that close and the record inside one `_mutation()`, so this backend
holds no archived card beside a staged request either. And because the refusal is the only thing
a caller can act on, it says which of the two facts is true: on Kanboard the effect may have
outlived its record and the caller still gets `audit_pending` — "backend write committed; audit
repair is required", exit status 4, the entry point to `recover_*` — while here the same failure
answers an ordinary refusal that carries no repair obligation, because a caller told to await a
repair would be waiting for one `reconcile` can never perform. On Kanboard all of these states
remain real and so do their recovery paths; that is a difference between the two backends, not a
difference in what a caller may ask for.

The namespace is the part most easily lost by accident, so it is worth saying plainly what would
have gone wrong without §3.9's `requests` table. Had each table owned its own `request_id UNIQUE`,
a caller could have used one id for a task comment, the same id for a sprint comment, and the same
id again for a budget charge, and all three would have committed. Today `TaskAudit` refuses the
second of those. A store that accepted it would be quietly weaker than the file journal it
replaced, and the weakness would surface as duplicated work after a retry rather than as an error —
which is exactly the failure DoD 5 is about.

`pending-audit/` and the staged transaction documents must be **empty at cutover**, not migrated: a
pending record describes a half-applied Kanboard write, and there is nothing in PostgreSQL for it
to be half-applied to. `export_board`'s existing refusal to export while any pending record exists
is exactly the precondition, and the cutover card should use it as its gate. After cutover the same
gate reads `requests WHERE status = 'staged'`, which by §3.9 can only be an unsettled shape-B
obligation — a closeout or a launch — and never a half-applied board write.

The audit journal itself — `state/board/events.ndjson` — keeps being written, generated from
`board_events` by the checkpoint. `board/analytics.py` reads a sealed copy and is untouched.

No live reader may take that generated file for the audit. `tasks.task_audit_for(client, data_dir)`
is the one place that decides which audit owner a process uses, and it decides from the client the
switch built: `SqlTaskAudit(client)` on PostgreSQL, `TaskAudit(data_dir)` on Kanboard. **SQL is the
canon on PostgreSQL and the file journal is the canon on Kanboard**, and a reader states which one it
is reading rather than inferring it from what happens to be on disk. `TaskWriter`,
`SprintReader`/`SprintWriter`, `ProductIssueStore`, the dispatcher runtime and its command host all
take it from there. The dispatcher built `TaskAudit(data)` beside a PostgreSQL client until
2026-09-10: a worker's `report:done` committed in `requests` was then invisible to the report wait,
the worker was declared stalled, and the observer got no wake for the Blocked move
(sprint:1437, secretary-1614).

Every other live reader follows the same rule since secretary-1622, and the list is enumerated in
`tests/test_architecture.py::FileAuditOwnershipTests` rather than kept in prose:

| Reader | What it reads on PostgreSQL |
|---|---|
| `CheckpointWriter` — the publication gate | staged `requests` rows; a card client that cannot be established blocks the checkpoint by name, and the Kanboard-only staged Product/Issue journal is asked for only on Kanboard, as `export_board` already does |
| `secretary task verify-audit` | the same staged count, with the backend named in the answer; the exit contract (0 clean, 1 pending) is unchanged |
| `CommandReadLayer.command_history` / `command_request` | committed and staged `requests`; an audit that cannot be read is `unavailable`/`unknown` and never an empty history or `not_found` |
| `webproto.ops` product-run publication | the `requests` row of the generic `product_run.*` record, in the store its own card client names |
| `BoardEventCanon` | the audit its caller's client named; a canon with neither an audit nor a data directory refuses instead of guessing one |
| `SprintReader` / sprint status reads | `requests` through `task_audit_for`; the shared traversal `_AuditOnce` has no data-directory construction at all |

Three file-audit constructions remain deliberate and each is named with its reason in that test: the
selector's own Kanboard branch, the pre-v2 pending-layout gate and unmigrated-claim check in
`product_issues.py` (statements about the file layout itself, run *because* the client is
PostgreSQL), and the default of a command host built with no audit, which only tests do.

One reader is deliberately *not* in that list: `webproto.journal.EventJournal`, which pages one
card's slice of `board/events.ndjson` by byte offset for `task_events`. Its cursor is a position in
that file — a released protocol value, not an implementation detail — so serving it from SQL is a
paging contract of its own and belongs to its own card, not to the selection this section describes.

### 7.4 Schema versioning and migrations

**SQLAlchemy models as the schema, Alembic as the migration tool.** The owner decided this on
2026-09-07, replacing this section's earlier decision (a hand-written runner over numbered `.sql`
files, its own `schema_migrations` table and a checksum rule, argued for on the grounds that it
added no dependency). That runner is not in the product; SQLAlchemy and Alembic are core
dependencies beside `psycopg[binary]` (§5.8).

- **The schema is `src/secretary/board/schema.py`** — declarative SQLAlchemy models, one per table
  of §3. §3 above stays what it is: the description of the target schema, written as DDL because
  DDL is what a reader can argue with. The models are that description made executable, and the
  two are held together by execution rather than by good intentions: the integration suite runs
  the migration against a real `postgres:16`, counts what §10 counted, and then asks Alembic to
  autogenerate a diff between the built database and the models — an empty diff, or a red test.
- **What an ORM does not express is expressed anyway, explicitly.** Every closed vocabulary of
  §3.12 is a `CheckConstraint`; the four partial unique indexes of §3.6 and §3.8 are `Index(...,
  postgresql_where=...)`; the generated `ref` columns are `Computed(..., persisted=True)`; §3.3's
  two scoped sprint cursors are `DEFERRABLE INITIALLY DEFERRED` composite foreign keys. Nothing
  §3 constrains is left to the application layer.
- **The version table is Alembic's `alembic_version`**, and there is no second one. It is the
  22nd table §10 counts and carries the 22nd primary key, exactly where `schema_migrations` used
  to stand, so every number in §10 is unchanged. No checksum rule is layered on top of it: an
  installation ahead of the tree is Alembic's own error to raise, and inventing bookkeeping beside
  a migration tool's is what this section no longer does.
- **The revisions live in `src/secretary/board/migrations/versions/`** and ship with the package.
  `0001_initial` builds an empty database in §3.13's order — step 1 creates the tables in §3
  reading order plus §9's `sprint_number_seq`, step 2 adds the constraints whose targets are
  forward or mutual references — and ends with §5.5's roles, grants and `ALTER DEFAULT
  PRIVILEGES`, which is why those reach every table above them and every table a later revision
  adds. A revision runs inside its own transaction (`transaction_per_migration`); PostgreSQL has
  transactional DDL, so a failed revision leaves the schema it was moving from, not half of one,
  and there is no down migration for the initial revision at all.
- **The connection is never an `alembic.ini` literal.** There is no `alembic.ini` in this
  product. `secretary.board.migrate` builds Alembic's `Config` in code and hands `env.py` the
  connection it opened from `board_store.resolve` (§5.4), as the `secretary_owner` role (§5.5).
  That is the only way in, and `env.py` refuses an invocation that supplies no connection rather
  than opening one of its own: the runner holds the advisory lock on that session for the whole
  apply, so a second connection would migrate outside the lock. `board_store.resolve` is also
  where the git-exclusion lifecycle of `board-store.env` is enforced, so no route to a configured
  store can migrate on top of credentials the instance repository is tracking.
- **The two generated passwords §5.5 needs are parameters of the run**, read from
  `board-store.env` and passed in Alembic's `config.attributes`; they are never bytes of a
  revision file, and PostgreSQL takes no bound parameter in `CREATE ROLE`, so the revision renders
  them through the dialect's own literal processor.
- **The advisory lock stays.** Alembic has no opinion about two upgrades racing on one
  installation, so `secretary.board.migrate` takes `pg_advisory_lock` on a fixed key on the same
  session the revisions run on, and releases it once at the end whatever happened in between.
- **Every process asserts the expected revision at startup** and refuses to write on a mismatch —
  the same shape as `board_transport`'s refusal of a partial configuration and `TaskAudit`'s
  `upgrade_required` refusal of the pre-v2 pending layout. A dispatcher writing through a schema
  it does not know is worse than a dispatcher that stops.
- **Where it runs.** `upgrade.py` owns ordered idempotent steps with typed results (`StepResult`),
  and `step_board_store` is one more of them, placed after `step_dependencies` (§5.8) — the
  dependencies have to exist before anything can connect — and before any service restart.

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
| `blocked_by` | task metadata (single ref) | `task_dependencies` row: `depends_on` always, `depends_on_task` when the board holds that card (§3.5, §8.6) |
| `supersedes` | task metadata (single ref) | `task_supersessions` row |
| `sprint_ref` | task metadata | `tasks.sprint_ref` FK — the same spelling as the metadata key, since 2026-09-07 made the sprint's reference its identity (§3.3, §9) |
| `record_type` (`task`/`issue`/`product`) | task metadata | table identity: the three record types become three tables |
| column name (`Ready`, `In progress`, …) | Kanboard column | `tasks.state`, via `_STATE_BY_COLUMN` |
| `is_active = 0` | Kanboard row status | `tasks.archived` |
| `swimlane` | Kanboard swimlane, per product | the serving lane is derived from the relational product; the independently observed metadata value is kept and returned from `extensions` as provenance (§8.2), without choosing placement — `tasks.extensions` for a card, `issues.extensions` for an Issue since 2026-09-07, and `products.extensions` for a Product since `0004` |
| `sprint_goal`, `sprint_definition_of_done` | sprint metadata | `sprints.goal`, `.definition_of_done` |
| `sprint_repositories` | JSON array of paths | `sprint_repositories` rows (§8.3) |
| `sprint_product`, `sprint_issues` | metadata / JSON array | `sprints.product_id`, `sprint_issues` rows |
| `sprint_reservations` | JSON array of project ids | `sprint_projects` rows with `reserved = true` (§4) |
| `sprint_status` | metadata | `sprints.status` |
| `sprint_budget`, `sprint_budget_uncharged` | JSON counter objects | `sprint_budget_events` rows; the counters become an aggregate (§8.7) |
| `sprint_current_task` | metadata | `sprints.current_task_ref`, scoped to the sprint by composite FK (§3.3) |
| `sprint_resume` | JSON object, six fields | one `sprint_resumes` row, columns |
| `sprint_source_audit` | JSON | `sprints.source_audit` jsonb (J2) |
| `sprint_observer`, executor pins | encoded strings | `sprints.observer` jsonb (J1), `worker_pin`, `reviewer_pin` |
| `product_id`, `product_projects` | metadata / JSON array | `products.product_id`, `product_projects` rows; other Product keys stay in `products.extensions.kanboard` |
| `issue_product`, `issue_kind`, `issue_priority`, `issue_closed_reason` | metadata | `issues` columns with CHECKs |
| `reference_repair` | metadata provenance | `tasks.extensions` (it describes a Kanboard-era repair) |
| `[role]\nbody` comments | Kanboard comments | `task_comments` / `sprint_comments` / `issue_comments` / `product_comments`: first line parsed once at import into `marker`, remainder into `body` |
| `[report:done]`, `[report:blocked]` with a `classification:` line, `[review:green]`, `[review:red]`, `[decision:release]`, `[decision:rework]`, `[decision:reslice]` | Kanboard comments rendered from typed events | `board_events` rows (`data` carries `marker`, `body`, `status`, `classification`, `decision`) **and** a `task_comments` row; the event is the fact, the comment is its rendering, exactly as `EventKind`'s comment already says |
| `[secretary-product-issue-transaction:<digest>]`, `[secretary-sprint-transaction:<digest>]` | Kanboard comments used as transaction witnesses | **not** carried forward as comments: they exist because Kanboard has no transaction. They import into `requests` as settled rows, with their digest retained for traceability |

**Import rule for the comment prefix.** The `[marker]` line is stripped exactly once, at import,
and only when the first line is a complete `[…]` on its own line and the token matches a known
marker vocabulary (a role in `_ROLES`, or `report:*` / `review:*` / `decision:*` / `issue:*` /
`sprint:resume` / `archive` / `rejected` / `validate:*` / `claim:*` / `watchdog:*` /
`steward:blocked-done` / `provision:request`). A first line that merely looks like a marker but is
not in the vocabulary keeps the whole body verbatim and gets `marker = NULL`. This is stricter than
today's `_normalize_comment`, which treats *any* bracketed first line as a marker, and it is
stricter in the safe direction: it can only fail to recognize a marker, never eat a line of prose.

**The five families this list did not have (2026-09-07).** The import found **778** comments whose
bracketed first line was outside the vocabulary above, so all 778 kept their whole body and got
`marker = NULL` — nothing was lost, but the vocabulary was incomplete against the board it
describes. The five families are named above and counted here, from a re-read of both live boards
on 2026-09-07:

| Family | Tokens on the board today | Comments |
|---|---|---|
| `validate:*` | `ci-green` 297, `review-return` 143, `automerge` 131, `ci-red` 17, `review-skipped` 4, `stand-green` 4 | 596 |
| `claim:*` | `started` 132 | 132 |
| `steward:blocked-done` | — | 34 |
| `watchdog:*` | `retry` 15 | 15 |
| `provision:request` | — | 1 |

The families are open (`validate:*`, `claim:*`, `watchdog:*`) where the board already carries more
than one token and closed (`steward:blocked-done`, `provision:request`) where it carries exactly
one, because a family that has grown once will grow again and a single-token marker is a name.
`steward:blocked-done` is deliberately not read as the role `steward`: the role marker is bare.

**And the number that closes it: zero.** The same read enumerated every bracketed first line on the
1514 Pipeline rows and the 102 sprint rows — 15 596 and 2 207 comments — and grouped them by token.
With these five families added, **no token on either board is outside the vocabulary**: 0 comments
remain uncovered. The 27 tokens the two boards carry are the roles `dispatcher`, `po`, `observer`,
`steward`, `worker`, `reviewer`; `report:done`, `report:blocked`; `review:red`, `review:green`;
`decision:rework`, `decision:release`, `decision:reslice`; `issue:closed`, `issue:priority`;
`sprint:resume`; `archive`; and the ten tokens of the five families above. `rejected` is in the
vocabulary and is currently unused on the board, which is not a defect: the rule can only fail to
recognize, and a vocabulary entry with no rows costs nothing.

### 8.2 Metadata keys the model does not name

`tasks._normalize` already collects every key outside `_KNOWN_METADATA` into
`extensions["kanboard"]`. The importer does the same into `tasks.extensions` (J3) — and, since
2026-09-07, into `issues.extensions` (J6) for an Issue row. This is what
keeps "simplify the schema and lose records" from happening by accident: a key nobody remembers
writing survives the import and is visible in a query, rather than being dropped because it was not
in a hand-written column list. The dry-run report of the importer card must list, per key, how many
rows carry it and where it landed — a key that lands in `extensions` for thousands of rows is a
missing column, and the report is how that gets noticed before cutover, not after.

**Why the Issue got one (2026-09-07).** The first import of real data reported per key for a card
(`swimlane` 399, `steward_report` 51, `model` 3, `reference_repair` 2, none of them near thousands,
so the alarm above correctly did not fire) and then had nothing to report for an Issue, because
there was no column: nine leftover task-metadata keys on 158 Issue rows and a lane that is not the
product's on 72 more had no home at all and would have been dropped. `issues.extensions` closes
that, and the per-key count this section requires now covers both tables — an Issue key that
appears on hundreds of rows is the same missing-column signal a card key is.

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
association is reachable today through its sprint (`tasks.sprint_ref` → `sprint_issues.sprint_ref`),
which is what the current product actually uses. Populating `task_issues` per card is a product
change and belongs to a later card, not to the import.

The join is spelled on the reference because `0002_board_gaps` made the sprint's reference its
identity (§3.3, §9); `tasks.sprint_number` never existed after that revision, and this sentence
named it until 2026-09-07. Unlike §4 and §7.1 it is prose and not a fence, so §10's extraction
could not catch it — which is worth saying plainly: executing the document finds every defect in
the statements it runs and none in the sentences around them.

### 8.5 Sprint close decisions have no board home

Per §2.6 gap 2, a close's decisions live in `<data>/board/product-issue-transactions/v1-*.json` and
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
  SQL serving always derives placement from the relational product, while metadata reads return
  the observed `extensions.kanboard.swimlane` value as provenance; that value never overrides the
  derived lane.

**Three fields the board leaves empty where the schema demanded one (2026-09-07).** These are not
absent *fields*; they are records the first import of real data could not write, and the choice
each one forced is stated here rather than only in the models. The first two were found by the
run of `secretary-1583` and closed by revision `0002`; the third was found by the parity run of
`secretary-1585` on 2026-09-07, which was honestly red over exactly one record of 944, and is
closed by revision `0003`.

- **A card with no `project`.** `secretary-583` carries no `project` metadata at all, and
  `tasks.project_id` was `NOT NULL`, so the card was named in the import report as unwritable.
  The column is nullable now. The two alternatives were both worse: deriving the project from the
  reference's prefix invents a fact the board does not state, and keeping the value in
  `extensions` would leave the row unwritable anyway, since the `NOT NULL` still has to be
  satisfied by something. A NULL says exactly what is true — the board does not say which project
  this card belongs to — and `UNIQUE (project_id, task_number)` simply does not constrain a row
  with no project, which is correct: there is no project whose numbering it could collide with.
- **A card with no `task_type`.** `secretary-583` again, and the same shape of finding: it
  carries no `task_type` metadata at all, and `tasks.task_type` was `NOT NULL` with a `CHECK` over
  `('code','research')`, so the parity run of `secretary-1585` reported
  `MISSING card secretary-583: task_type '' is outside the CHECK vocabulary` and refused the row.
  The column is nullable now and its `CHECK` admits NULL or a value of the vocabulary (§3.12).
  Defaulting to `code` was the alternative and was rejected for the reason the project was: the
  board does not say it, and the product's own reader does not say it either — `tasks._card`
  returns `_text(meta.get("task_type"))`, which is `''` for this card and not `'code'`. A NULL
  says what is true. So that a NULL is not mistaken for a value the import lost, the importer
  records the silence in the row itself: `tasks.extensions` carries
  `{"board_never_named": ["task_type"]}` beside §8.2's `kanboard` bag, and the report names every
  such card on its own line with the count. The question was put to the owner; if the answer is
  a different one, it is applied to exactly the rows that line names.
- **A dependency on a card the board does not hold.** Nine `blocked_by` values name cards that are
  not on this board (`triggered-agents-*`, `memory-mcp-*`), and `task_dependencies.depends_on` was
  a foreign key into `tasks`, so all nine were dropped. The table now carries the reference and
  the resolution separately (§3.5): `depends_on` is the reference as written and half of the
  primary key, `depends_on_task` is the foreign key and is set exactly when the card is on the
  board. Dropping the foreign key altogether was the alternative, and it would have cost every
  dependency its relational check to keep nine; putting the unresolved ones in `extensions` was
  the other, and it would have hidden a *relation* inside a provenance bag where no join can
  reach it. `depends_on_task IS NULL` is the query for "not on this board", and
  `dependency_resolution_is_the_same_reference` forbids the two columns from naming different
  cards.

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
| `sprint:N`, and any other `sprint:` reference | `reference` on the sprint row; N allocated by `next_reference` over open **and** archived rows under `reference_allocation_lock` | `sprints.ref` PK — the reference itself, stored verbatim; `sprints.sprint_number` a nullable `UNIQUE` integer. New numbers still come from `sprint_number_seq`, set past the imported maximum at import, and the unique index on the number — not a file lock — is still what makes reuse of a number impossible, which is the defect `references.py` documents for 2026-08-06. Changed 2026-09-07: see below. |
| `<project>-<n>` task refs | `reference` on the card row; same allocator, same lock, same archived-rows rule | `tasks.task_ref` PK plus `UNIQUE (project_id, task_number)`; one sequence per project, or `max(task_number)+1` under the row lock the insert takes anyway. The 2026-08-18 defect (a reference derived from a fresh row id colliding with an archived card) cannot recur: archived rows are ordinary rows in `tasks`. |
| `product:<id>` | `product_id` metadata + `reference` | `products.product_id` PK, `products.ref` generated `UNIQUE` |
| `issue:<hash>` | `reference`, hash-allocated (not numbered) | `issues.issue_id` PK, `issues.ref` generated `UNIQUE` |
| sprint↔issue links | `sprint_issues` JSON array | `sprint_issues` table, FK both ways |
| sprint↔project links | `sprint_reservations` JSON array + derived guard index | `sprint_projects` (§4) |
| sprint↔repository links | `sprint_repositories` JSON array of paths | `sprint_repositories` → `repositories` (§8.3) |
| card→sprint | `sprint_ref` metadata | `tasks.sprint_ref` FK, into `sprints.ref` |
| card→card dependency | `blocked_by` metadata | `task_dependencies.depends_on` (always) and `.depends_on_task` (the FK, when the board holds that card — §8.6) |
| supersession | `supersedes` metadata | `task_supersessions` |
| archive | Kanboard `is_active = 0`, `closeTask` | `tasks.archived`; the row is never deleted |
| issue close reason | `issue_closed_reason` metadata | `issues.close_reason` + the state CHECK |
| sprint close reason and account | close event payload + knowledge closeout | `sprints.close_reason`, `sprints.closeout_document` (the path), `sprint_decisions` (§8.5) |
| card decision (`release`/`rework`/`reslice`) | `[decision:*]` comment + `CARD_DECIDED` event | `board_events` + `task_comments.marker` |
| worker report and review verdict | `[report:*]` / `[review:*]` comments + events | same |
| budgets | `sprint_budget` JSON counters | `sprint_budget_events` + derived totals (§8.7) |
| `request_id` | `TaskAudit` committed/pending records over one journal index, plus the Product/Issue transaction documents it cross-checks | `requests.request_id` PRIMARY KEY — one namespace for the installation. `board_events`, `task_comments`, `issue_comments`, `product_comments`, `sprint_comments`, `sprint_budget_events` and `sprint_decisions` all reference it (§3.9); the first six are additionally unique per request, `sprint_decisions` is not, because one close claims one id and writes many decisions |
| `event_id` | `events.ndjson` records | `board_events.event_id` PK |
| head run reference | `Actor.head_run_ref` on events | `board_events.head_run_ref` (a reference only; the head registry stays out of this schema) |

Every reference in the left column survives cutover with the same spelling. Nothing in this schema
renumbers, rewrites or re-derives an existing `sprint:N` or task ref.

**Why the sprint's identity moved, and the one reference that still does not fit (2026-09-07).**
The claim in the paragraph above was not true of this board until now. The first import of real
data found two live sprint rows whose reference carries no number — `sprint:canary-terra-20260813`
and `sprint:canary-terra-final-20260813` — which `sprint_number integer PRIMARY KEY` could not
hold; `secretary-1438` and `secretary-1439` lost their link to a sprint as a consequence. Making
the reference the primary key (§3.3) makes the claim true for both, keeps `sprint:N` spelled
`sprint:N`, keeps the allocator, and moves every scoped composite key onto the reference without
narrowing what any of them checks.

One record is still not representable, and it is named rather than quietly dropped:
**`sprint:1037` is on the board twice** — a live row (Kanboard task 748) and an archived row
(task 1037, whose title is the one `sprint:canary-terra-20260813` now carries). A primary key on
`ref` holds one of them, exactly as the integer key did. Three ways out exist and the choice is the
owner's, not this card's:

1. **Disambiguate the archived row at import** — the archived row is stored with a distinguishing
   reference and its original spelling is kept in `sprints.source_audit` as provenance. Both
   *records* survive with every field, comment and card link intact; one archived row loses its
   spelling, and §9's promise above holds for every live row. This is the cheapest option and the
   one this card would recommend, and it needs no schema change at all: two sprint rows are
   representable today as long as their references differ.
2. **Surrogate key** — `sprints` grows a `bigint` identity primary key, `ref` becomes unique only
   among non-archived rows, and every scoped composite key targets the surrogate. Both rows keep
   their spelling; the cost is that the reference stops being the identity again, every foreign key
   in §3.3, §3.4, §3.5, §3.6, §3.7 and §3.8 carries a number the board does not know, and
   "a reference names one sprint" stops being a database fact.
3. **Drop the uniqueness of `ref`** — not viable: every scoped key in this schema targets it, and
   a duplicate reference would make `tasks.sprint_ref` ambiguous.

Until that is decided, the schema does what the integer key did — accepts one row per reference —
and the importer's report names the archived `sprint:1037` as the one record it cannot carry.

---

## 10. How the executable parts of this document are verified

Every SQL statement and shell command above is executed before this document is published, in the
order §3.13 and §5 prescribe, against a **throwaway `postgres:16` container** — never against the
live installation, which this card only reads. **When the container and the document disagree, the
container is right and the document changes.**

This exists because reading is not enough. Two earlier revisions of this document each shipped a
statement PostgreSQL refuses — a `-U secretary` connection for a role no longer created, and an
`INSERT … status = 'committed'` without the `settled_at` its own `CHECK` requires — and both were
found by executing them, not by review. Every defect of that class is cheap to catch and invisible
to careful reading.

The procedure, which is the document's own order:

| Step | What runs | As |
|---|---|---|
| 1 | `docker run postgres:16` with the §5.2 `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | — |
| 2 | the §5.5 `CREATE ROLE` and `GRANT` fence | `secretary_owner` |
| 3 | every §3 `CREATE` fence, then every §3 `ALTER` fence, in §3.13's two steps | `secretary_owner` |
| 3c | §4's restatement of §3.6's partial unique index, compared byte-for-byte with the original instead of created twice | — |
| 4 | prerequisite rows a sprint create references: a product, its repositories, its issue | `secretary_owner` |
| 5 | the §7.1 canonical sprint-create transaction, verbatim, binds substituted | `secretary_app` |
| 6 | the §3.1 registry-projection upserts, run twice, to show the projection is idempotent and that a removed binding sets `registry_present = false` rather than deleting a row | `secretary_app` |
| 7 | one negative probe per constraint this document claims (§4, §3.3, §3.5, §3.8, §3.9, §3.12, §5.5), and one positive probe per acceptance it claims | `secretary_app`, `secretary_read` |

`pg_dump` and `pg_restore` are **not** in this run. §5.7 leaves the dump an open question, so there
is no statement of this document's for them to verify; the card that answers §5.7 runs them against
the code it ships.

Step 7 is what keeps a constraint from being decorative. A document may state that a partial unique
index forbids a second live reservation; only executing the second insert shows the index is
actually reachable, correctly predicated and attached to the right column.

The statements are extracted from this file rather than retyped, so what runs is what is published.
A fence is assigned to a step by its section first and its first keyword second: in §3, a `CREATE`
fence is step 3a and an `ALTER` fence is step 3b; §5.5's fence is step 2; §7.1's `BEGIN;` fence is
step 5; §3.1's projection fence is step 6; §3.9's three lifecycle fences and §4's release `UPDATE`
are step 7; §4's repeated index is step 3c. Section before keyword, because §5.5's fence also
begins with `CREATE` and is not part of §3's DDL. That ordering
is also why §7.1 spells its inserts out instead of eliding them as `(...)`: a placeholder cannot be
executed, and an example nobody executes is exactly where the previous two defects lived.

**The fence accounting, so a later card can check it rather than trust it.** This document contains
**22** fenced SQL blocks. Twenty-one of them open at column 0 and one — §3.1's registry-projection
fence, line 514 of this revision — is indented three spaces because it sits inside a numbered list
item. A count
that anchors the fence to the start of the line therefore returns 21 and silently misses that one:

```bash
grep -c '^```sql'            docs/BOARD_STORE.md   # 21 -- misses the indented fence
grep -cE '^[[:space:]]*```sql' docs/BOARD_STORE.md  # 22 -- the real total
```

Of those 22, **21 were executed** and exactly **one deliberately was not**: §4's restatement of
§3.6's `sprint_projects_one_live_reservation` index. It is a repetition, not a second statement, so
running it would only raise "already exists"; the check it actually needs is that the two spellings
have not drifted apart, and the run does that by comparing them byte for byte. No other fence is
illustrative: every remaining one, including §3.9's three lifecycle fences and §4's release
`UPDATE`, was run verbatim with its `:name` binds supplied. That is 21 executed + 1 compared = 22, and the
transcript quoted in this round's report has a line for each.

The 2026-09-07 result was a database of 22 tables carrying 37 `CHECK`, 38 foreign-key, 22
primary-key and 13 unique constraints and 4 partial unique indexes, before `0004`. The current
mandatory migration/schema gate counts 24, 37, 40, 24, 16 and 4 including `alembic_version`, and
also runs Alembic `compare_metadata`. All 18 original negative probes were refused, each by the
constraint or the privilege it names; all 13 positive probes were accepted.

**What the 2026-09-07 re-run found (`secretary-1585`).** `0002_board_gaps` changed §3 and left
three places elsewhere naming a column it had removed, and executing the document is what separated
the two that are statements from the one that is prose. §4's release `UPDATE` and §7.1's canonical
sprint-create transaction both named `sprint_number` on tables whose scoping column is now
`sprints.ref`, and both were refused by PostgreSQL — the third and fourth defects of exactly the
class this section exists to catch. §8.4's `tasks.sprint_number` was the same mistake in a sentence,
and no extraction could have caught it, because §10 executes the statements a document makes and
not the claims it makes around them. That is the honest boundary of this procedure, and it is worth
stating where the procedure is described: running the document proves the SQL, and nothing else.

**What the 2026-09-07 parity run found (`secretary-1586`).** That run changed exactly one fence
of this document, §3.5's, and one line of it: `tasks.task_type` is nullable and its `CHECK` is
named and restated. Re-running the whole extraction would have re-proved 21 unchanged fences, so
what ran instead was §3.5's fence alone, extracted from this file and executed verbatim against a
throwaway `postgres:16` with stub `projects`, `sprints` and `issues` — plus one positive probe (a
card with neither a project nor a type is a row) and one negative probe (`task_type = 'chore'` is
still refused, by the named constraint). PostgreSQL reports the constraint back as
`CHECK (task_type IS NULL OR task_type = ANY (ARRAY['code','research']))`, which is what
`0003_task_type_optional` builds and what `tests/test_board_store_schema.py` counts, so the
document, the models and the migration agree. Naming what did **not** run is the point of saying
this: the other 20 executed fences were not re-executed in that round, and the whole-document run
this section describes remains the thing a card that changes §3 broadly has to do.

The five probes added in the `secretary-1585` re-run are the five things `0002` made true and
`0001` did not: a
comment on an Issue has a table and a generated `issue_ref`, a card with no project is a row, a
dependency naming a card the board does not hold is a row, and a reference and its number may not
disagree. Each is a positive probe except the last, which is negative, because that is which
direction each of them claims.

The container is verification, not delivery. Nothing here stands the store up for use, imports any
data, or touches the live installation; standing the schema up for real is the first implementation
card's job. The container and the extraction script are deleted afterwards; neither is added to the
repository.

---

## 11. What this document does not decide

- The SQL migration files, the importer, the dry-run report format, the parity check and the
  cutover procedure. Separate cards.
- Backup and restore of the PostgreSQL store: the artefact format, its slot in the archive, the
  restore order and the role bootstrap after a restore, and the changes to `backup.py`,
  `backup_policy.py`, `backup_verify.py` and `docs/RECOVERY.md` that any of it needs. §5.7 states
  the three facts that constrain the answer and hands the question to a later card that ships the
  code with it.
- Containerization of the application, the web transport, the dispatcher or the workers, and the
  removal of Orca. A separate slice the owner has not approved.
- Any change to behaviour, UI, admission rules or project selection.
- Moving agent runtime, memory, profiles, secrets or providers.
- Any change to Kanboard or to the data now in it.
- Updates to `ARCHITECTURE.md`, `PROTOCOLS.md`, `OPERATIONS.md` and `TESTING.md`. Those are updated
  when the mechanism exists, not from a design document.

## Complete audit import

`secretary board import --data-dir ...` treats `board/events.ndjson` as part of the source, not as
a budget side channel. It streams every nonblank row, requires a JSON object with unique nonempty
`event_id` and `request_id`, and preserves that object unchanged in `requests.intent`. The request
operation is the journal `kind`; `protocol`, subject kind/reference and source timestamps are
derived from the record. Generic records remain request rows only. A row declaring
`record_type=board.protocol_event` must pass `Event.from_record`; only that closed vocabulary is
projected into `board_events`.

Budget charges link to the journal-owned request claim. They do not manufacture a second request
row or replace its intent. A counter with no surviving journal row retains the documented
synthetic, approximate claim. Any conflicting reuse of an installation-wide request id refuses the
plan.

A real import with `--data-dir` takes two complete observations of both Kanboard boards, registry,
journal and transaction documents. The report records the before and after SHA-256 identities and
the journal device, inode, size, mtime and content digest. Descriptor movement during streaming,
path replacement, truncation, append, or any difference between the observations refuses before a
database connection is opened. Retry reads a new pair of observations. This is a consistency fence,
not a claim that a dispatcher pause is a write barrier.

The current importer remains an empty-target operation. A second application refuses atomically
and leaves the first import unchanged. This evidence does not switch the configured backend or
perform live cutover.
# Controlled activation

The presence of `board-store.env` provisions connection material but selects nothing.
`SECRETARY_CARD_BACKEND` in protected `runtime.env` remains the sole selector; absence means
`kanboard`, and an unknown value fails closed. `secretary cutover` provisions and verifies an empty,
schema-current PostgreSQL target, takes the complete audit-aware two-observation Kanboard source
fence, imports once, and requires every parity axis before atomically writing
`SECRETARY_CARD_BACKEND=postgres`.

The selector is propagated through the shared role allowlist to pipeline, observer, worker,
reviewer, steward, retro and curator processes. Dispatcher and web units already consume the same
`runtime.env`. No consumer infers a backend from the database configuration file and there is no
dual-write or read fallback.

The old Kanboard store is retained as a protected read-only archive. It is eligible for pre-first-
SQL-write rollback only while its frozen fingerprint still matches; after a committed application
event it is never a writable recovery target.

A successful pre-import recovery preserves its terminal controller evidence in
`<data_dir>/cutover/history` before releasing the canonical controller slot. A later plan binds that
archive and receives a distinct identity. Once final import completes, target occupancy takes
precedence over first-application-write status: that identity, plus PostgreSQL-only, uncertain and
completed identities, stays canonical and cannot create a new Kanboard attempt.

Activation acceptance uses public commands for Product, Issue, Sprint and Task reads and writes.
It proves sprint-comment delivery and replay, task request replay and history lookup,
reservation/claim release, completion, archival and post-close comment behavior, read-only web
surfaces and an isolated dispatcher tick. The post-switch checkpoint and full backup must preserve
the acceptance task, contain a `postgres_dump` component and contain no `raw_board` component.
These are requirements on each external cutover's evidence, not a claim that the live installation
has already been cut over.

### Successor target generation

The only supported successor for an occupied completed import is database rotation inside the same
PostgreSQL cluster and volume. The imported database is fenced and renamed to a bounded name derived
from its controller plan and stable database OID. A distinct empty database is created under the
unchanged configured name and owner. The cluster-wide app/read roles and credentials are reused only
after their attributes are verified; schema grants and owner default privileges are installed again
for the new database, including sequence `USAGE`. No row-level merge/upsert importer exists.

The archive is evidence, not a fallback backend. Ordinary roles cannot connect to it, the selector
continues to name Kanboard, and the configured PostgreSQL name can only denote the new empty OID. Its
native dump, checksum, old/new OIDs, schema, import counts and audit boundary live in immutable cutover
history. The preserved database must be externally upgraded from `0006` and verified at `0007` before
rotation; the successor lifecycle does not migrate its rows. An immutable release receipt binds that
history checksum, predecessor plan, archive OID/name and dump checksum before the evidence can become
a predecessor in the next plan.
