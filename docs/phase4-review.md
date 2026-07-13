# Phase 4 Review

Phase 4 goal: move memory out of the separate `panelmem-kb` repo into
`secretary-data/memory`, with `secretary memory` as the single writer path.

This card is a research phase-review, not an implementation review. Phase 1-3
reviews checked commands that already ran on the host. Phase 4 storage is not
built yet, so this document fixes the executable contract before any live
storage changes: the on-disk journal, concurrency rules, import, the writer
protocol, and a reversible cutover. It resolves the architectural forks so the
implementation cards do not have to.

## Current State

The pieces this phase reshapes already exist:

- `panelmem-kb` is the canon today: flat markdown facts under
  `~/panelmem-kb/memory/<global|project-dir>/<slug>.md`, one fact per file, a
  plain git repo. 197 facts across 16 top-level dirs at the time of this review
  (`global` plus one dir per project or system repo). It is its own git history
  that the curator pushes to as an off-box backup.
- `memory-mcp` owns the derived index. `server.py` reads the canon
  (`PANELMEM_KB/memory`), embeds each fact, and serves `memory_search`. A watcher
  thread reindexes in-process within seconds of a canon change
  (`canon_signature` -> `rebuild_index`). The sqlite index is derived and
  gitignored; `reindex.py` is the manual fallback with the daemon stopped.
- Scope comes from the top directory under the canon: `global` -> `global`,
  anything else -> `project:<dir>` (`server.scope_for`). There is no `project/`
  intermediate directory today.
- The curator is the only writer. Its runtime (`triggered_agents/agents/curator`)
  harvests transcripts and personal-memory files into a redacted batch; the
  curate skill then writes `.md` files and runs
  `git -C ~/panelmem-kb add -A && commit && push` by hand. The CLI helpers only
  move the harvest watermark, two-phase so a crash re-harvests instead of
  dropping turns.
- The data plane is already carved out. `data-manifest.json` declares
  `memory.facts = memory/facts`, `memory.export = memory/export.ndjson`,
  `memory.index = memory/index.sqlite`. `secretary data export-memory` copies
  `panelmem-kb/memory/*` into `secretary-data/memory/facts/*` and writes
  `export.ndjson`. That exporter is a read-only snapshot; Phase 4 turns
  `memory/facts` into the live canon and journal.

## Storage Layout and Metadata

### Directory layout

```text
secretary-data/memory/
  facts/                       # local git journal, git root is here
    global/<slug>.md
    <project-dir>/<slug>.md
  export.ndjson                # derived, sibling of facts, not journalled
  index.sqlite                 # derived, sibling of facts, not journalled
  manifest.json                # derived version/state, not journalled
```

The git journal root is `memory/facts`, not `memory`. Only canonical fact
markdown lives inside the repo. `export.ndjson`, `index.sqlite` and
`manifest.json` are derived siblings above the repo root, so a `git status` in
the journal never shows derived churn and the journal diff is always a fact
diff. `index.sqlite` and its `-wal`/`-shm` companions are rebuildable and are
never committed.

The fact layout stays flat: `facts/<scope-dir>/<slug>.md`, where `<scope-dir>`
is `global` or a project/system-repo directory name. This matches
`memory-mcp.scope_for` and the current `export-memory` copy. The design doc's
`facts/global` plus `facts/project` wording implies a `project/` intermediate
directory that does not exist and would break `scope_for`. Contract: no
`project/` level, one directory per scope.

### Fact file format

Frontmatter plus body, unchanged from the current canon so import is a copy, not
a rewrite:

```markdown
---
tags: [infra, orca]
source: curator:claude/1a2b3c4d
created: 2026-07-12
pinned: false
supersedes: old-slug-a,old-slug-b
---
Fact statement, short and self-contained.

Почему: only when there is a working invariant for a future agent.
```

- `tags`: list, serialized to a comma string in the index (`parse_fact`).
- `source`: logical writer of the fact content, for example
  `curator:claude/<session8>`, `curator:claude/memory/<slug>` for a fact lifted
  from personal memory, or `secretary` for an operator write. This is
  attribution, not authentication.
- `created`: fact date.
- `pinned`: optional, for always-relevant facts.
- `supersedes`: optional, comma-separated slugs this fact replaces (see below).

A fact id is `scope-dir/slug`, unique by path. `slug` is unique within its scope
directory; the service rejects a create whose target path already exists.

### Commit identity

The memory service is the single process that commits. Git author and committer
are a fixed service identity so `git blame` stays stable no matter which head
triggered the write:

```text
author = committer = "secretary-memory <memory@secretary.local>"
```

The logical principal (who asked for the write) is recorded in the commit
message, not the git identity:

```text
memory <op>: <scope-dir>/<slug> — <short summary>

Op: commit | supersede | import
Principal: curator:claude/1a2b3c4d
Supersedes: <scope-dir>/<old-slug>, ...   # supersede only
```

One logical write is one commit. `Principal` carries the acting role and, where
relevant, the head and session that produced the fact; it mirrors the fact
`source` field.

### Supersede rules

Supersede is a replace-and-delete in a single commit:

- The new fact carries `supersedes: <slug>[,<slug>...]` in frontmatter.
- The superseded `.md` files are `git rm`'d in the same commit that adds or
  rewrites the survivor.
- The body of the survivor states current state only. It does not narrate what
  changed; the history lives in the journal, not in the fact text.
- Cluster compaction (several non-conflicting cards folded into one) is the same
  operation with a multi-slug `supersedes` list.

Because old files are removed in-commit, the reindex after the commit drops the
superseded vectors automatically; there is no tombstone to clean up in the
index.

## Concurrency: Locking, Atomic Write, Failure

### Single writer, serialized

All writes go through the memory service. `secretary memory` subcommands call the
same service library; heads never write the journal directly. Writes are
serialized by an advisory lock file `secretary-data/memory/.write.lock`
(`flock`), held for the whole propose-to-commit or supersede transaction. A
second writer either blocks or fails fast with a distinct non-zero exit rather
than racing on the working tree. This reuses the advisory-lock pattern already
in `backup create`.

### Atomic file write

Each fact file is written to a temp file in the same directory, fsync'd, then
`os.replace`'d into place, the same staging-then-replace approach `data.py`
already uses for exports. No reader ever sees a half-written `.md`.

### The commit is the transaction boundary

Within one lock hold: write or remove the fact files, `git add -A`, `git commit`.
The canon is defined as `git HEAD` of the journal. The working tree is only ever
transiently dirty inside a held lock.

### Interrupted write

Propose and commit are two phases, matching the curator harvest/advance model:

- `propose` stages a candidate (temp file plus intended op) under
  `memory/.staging/<propose-id>`; it does not touch the journal or the index.
- `commit` (or `supersede`) applies the staged change under the lock and makes
  the git commit, which triggers the reindex.

Recovery contract on service start:

- If `git status --porcelain` in the journal is dirty, a previous transaction
  died mid-write. The canon is the last commit, so recovery is
  `git reset --hard HEAD && git clean -fd` to discard the partial write. Discard
  is safe because an uncommitted change was never canon and can be re-proposed.
- Stale `memory/.staging/*` entries are cleared only when they are valid
  uncommitted proposals older than 7 days. A proposal with a recent `.active`
  marker is kept for at least 1 hour even when its original `created_at` is old.
  Completed proposals with `committed.json` are not GC candidates; they carry the
  journal commit for retry after a derived export failure. GC never
  auto-commits a proposal.
- A crash after commit but before reindex leaves a stale index. Canon (HEAD) is
  still correct; the watcher or a `memory reindex` rebuilds it. The index is
  never authoritative.

### Conflict

Same-slug writes cannot interleave because the lock serializes them. A logical
conflict (new fact contradicts an existing one) is resolved by the writing agent
at propose time via `memory_search` dedup, then expressed as a supersede. The
service enforces the mechanical guard: a create targeting an existing path is
rejected; replacing an existing fact must go through `supersede` or an explicit
update.

## Import, Fallback, Rollback, Old-Writer Sunset

### One-time import from panelmem-kb

```bash
secretary memory import --from ~/panelmem-kb/memory
```

Steps:

1. `git init` at `secretary-data/memory/facts` if absent.
2. Copy `panelmem-kb/memory/*` into `facts/*`, preserving the flat
   `global`/`<project-dir>` layout.
3. One import commit recording the source head:
   `memory import: seed from panelmem-kb @ <panelmem-head-sha>` with
   `Op: import`. The new journal history starts at this commit. The old
   `panelmem-kb` git history is not replayed; it stays in the archived repo.
4. Build `export.ndjson` and `index.sqlite` from `facts`.
5. Verify: journal fact count equals the source fact count, `export.ndjson`
   line count equals the fact count, and a `memory_search` smoke query returns
   hits.

Import is guarded against double-seeding: it refuses to run if the journal
already has commits past its import marker.

### Readonly fallback

`panelmem-kb` is left intact and readable. After cutover it becomes readonly: the
curator stops writing to it, but it is retained as both an archive of the old
history and a live fallback source. If the new storage fails a health gate,
`memory-mcp` can be pointed back at `panelmem-kb` via its `PANELMEM_KB` env and
reindex from it. Deleting or freezing `panelmem-kb` is Phase 9 and explicitly out
of scope here.

### Rollback

Cutover is reversible because it is a repoint plus a writer switch, not a
destructive migration:

1. Point `memory-mcp` `PANELMEM_KB` back at `~/panelmem-kb`, restart or let it
   reindex.
2. Re-enable the curator's old `panelmem-kb` write path.
3. Reindex.

The `secretary-data/memory/facts` journal is retained on rollback, never
destroyed, so a later retry does not re-import from scratch and no history is
lost on either side.

### When the old writer path is forbidden

The old curator write path (`git push panelmem-kb`) is disabled at the cutover
step, once the import commit exists, `memory-mcp` reads the new journal, and the
health gates are green. From that point:

- The curate skill uses `secretary memory propose/commit/supersede`, not direct
  file writes and `git push` to `panelmem-kb`.
- `panelmem-kb` is marked readonly.

Full enforcement (a `doctor` check that flags any direct write to the old repo,
the way Phase 9 flags direct board-backend writes) is Phase 9. Phase 4 stops the
curator from using the old path and marks the repo readonly; it does not add the
guard.

## Writer Protocol and Permissions

### CLI / API

`secretary memory` is the public writer path; the service library behind it is
the single writer. Verbs:

```bash
# write path (curator, secretary/operator)
secretary memory propose  --scope <global|project:X> --slug <slug> \
                          --file <fact.md> [--tags a,b] [--pinned] \
                          [--supersedes s1,s2]
secretary memory commit   --propose-id <id>
secretary memory supersede --scope <...> --slug <new> --file <fact.md> \
                          --supersedes s1,s2

# read path (all roles)
secretary memory search   --query <q> [--scope <...>]

# admin (secretary/operator)
secretary memory import   --from <dir>
secretary memory reindex
secretary memory verify
```

- `propose` stages a candidate and returns a `propose-id`; no journal commit, no
  index change. The agent may read it back to dedup before committing.
- `commit` finalizes a proposed new or updated fact into one git commit, which
  triggers the reindex.
- `supersede` finalizes a replace-and-delete in one commit with a `Supersedes`
  trailer.
- `search` is the read path; it delegates to `memory-mcp` so there is one
  ranker.
- `reindex`, `import`, `verify` are operator/admin operations.

The three ops (propose, commit, supersede) exist as service functions too; the
curator runtime calls them directly and the CLI is a thin wrapper. Either way the
service process is the only principal that writes files and makes commits.

### Permission matrix

| Principal | search | propose / commit / supersede | import / reindex / verify | direct fs/git to facts |
|---|---|---|---|---|
| ordinary heads (worker, reviewer) | yes | no | no | no |
| curator | yes | yes | no | no |
| secretary / operator | yes | yes | yes | escape hatch only |
| memory service (process) | n/a | executes | executes | yes, sole writer |

- Ordinary heads stay read-only, preserving "agents read, only the curator
  writes". They reach memory through `memory_search` over MCP.
- The curator is the normal writer, but only through the protocol. It no longer
  writes files or pushes git directly.
- The secretary/operator holds the admin ops and a development escape hatch:
  direct storage access when extending the protocol or fixing a migration, the
  same escape-hatch model the board backend uses, with the same Phase 9 sunset.
- The memory service process is the sole thing that mutates the journal, under
  its fixed commit identity.

## Cutover Plan

Side-by-side and reversible at every step. The old canon keeps serving until the
new one passes its gates.

1. Import into a fresh `secretary-data/memory/facts` while `panelmem-kb` stays
   live and the curator still writes to it.
2. Freeze the curator (pause the pipeline or disable the curator timer) so no new
   fact lands in `panelmem-kb` during the switch.
3. Re-run `secretary memory import` after the freeze so the journal captures any
   fact written since step 1. Import stays idempotent for this.
4. Parity gate: `secretary memory verify` green (journal count == source count,
   export lines == fact count, sample `memory_search` on the new index matches
   the old).
5. Cut `memory-mcp` over: point its canon env at
   `secretary-data/memory/facts`, restart or reindex.
6. Switch the curator to the `secretary memory` protocol and disable its
   `panelmem-kb` push. Mark `panelmem-kb` readonly.
7. Unfreeze. Run one end-to-end: a protocol write becomes a git commit in the
   journal and is found by `memory_search` after reindex.

### Health gates

All must be green to keep the cutover; any red triggers the rollback above.

- `memory-mcp` boots and its endpoint is ready before the embedding model
  finishes loading. The `memory-mcp-404` readiness defect is a hard dependency of
  the cutover step and must be fixed as part of this phase; a slow model load
  must not 404 the service.
- Reindex fact count equals the journal fact count (`git ls-files | wc -l`
  matched against the index count).
- One protocol write is visible as a git commit in
  `secretary-data/memory/facts`.
- `memory_search` returns that new fact after reindex.
- `git status --porcelain` in the journal is clean (no half-written
  transaction).

### Verification commands

```bash
python3 -m secretary memory verify
git -C ~/secretary-data/memory/facts log --oneline -5
git -C ~/secretary-data/memory/facts status --porcelain     # empty = clean
git -C ~/secretary-data/memory/facts ls-files | wc -l        # canon fact count
python3 -m secretary memory search --query "<known fact>" --scope project:secretary
```

None of these delete or rewrite either repo. `panelmem-kb` is neither removed nor
force-pushed; it becomes a readonly archive and fallback. Irreversible deletion
of the old repo is Phase 9 and out of scope.

## Forks Resolved

- Journal root is `memory/facts`, so derived files stay out of the repo.
- Fact layout is flat, `facts/<scope-dir>/<slug>.md`, no `project/` level.
- Commit identity is a fixed service identity; the acting principal rides in the
  commit message, not the git author.
- Writes are serialized by one advisory lock; atomic `os.replace` per file; the
  git commit is the transaction; a dirty tree at startup is discarded to HEAD.
- Import seeds a new journal from a copy plus one commit that records the
  `panelmem-kb` head; old history stays archived.
- Cutover is a repoint plus writer switch, reversible, with `panelmem-kb`
  retained as readonly fallback.
- Heads read, curator writes through the protocol, operator holds admin and the
  escape hatch, the service process is the sole file/git writer.

## Live Cutover Record

Run date: 2026-07-13. Operator card: `secretary-431`.

### Backup and rollback checkpoint

Checkpoint directory:

```text
/home/dev/secretary-data/backups/memory-cutover-20260713T001258Z
```

Contents:

- `secretary-data-memory-pre-cutover.tar.gz`: full pre-cutover
  `/home/dev/secretary-data/memory` archive.
- `panelmem-kb-pre-cutover.tar.gz`: old canon archive with
  `/home/dev/panelmem-kb/memory` and `.git`.
- `memory-mcp.service.pre-cutover`: installed unit before the live restart.
- `checkpoint.txt`: timestamp, old `panelmem-kb` HEAD and fact counts.
- `SHA256SUMS`: verified with `sha256sum -c`.
- `panelmem-kb-origin-push-url.pre-readonly`: old push URL, captured before
  disabling pushes.

Rollback commands:

```bash
chmod -R u+w /home/dev/panelmem-kb
git -C /home/dev/panelmem-kb remote set-url --push origin "$(cat /home/dev/secretary-data/backups/memory-cutover-20260713T001258Z/panelmem-kb-origin-push-url.pre-readonly)"
sudo cp /home/dev/secretary-data/backups/memory-cutover-20260713T001258Z/memory-mcp.service.pre-cutover /etc/systemd/system/memory-mcp.service
sudo systemctl daemon-reload
sudo systemctl restart memory-mcp
```

To force old-canon rollback rather than the saved unit shape, set
`MEMORY_CANON_ROOT=/home/dev/panelmem-kb/memory`, remove
`MEMORY_CANON_EXPORT`, set `MEMORY_DB=/home/dev/memory-mcp/memory.db`, reload
systemd and restart `memory-mcp`. The new
`/home/dev/secretary-data/memory/facts` journal is retained either way.

### Import and service cutover

Pre-import state:

- `/home/dev/secretary-data/memory/facts`: 208 markdown facts, no `.git`.
- `/home/dev/panelmem-kb/memory`: 232 markdown facts.
- Installed `memory-mcp` unit had no `MEMORY_CANON_ROOT`,
  `MEMORY_CANON_EXPORT` or `MEMORY_DB`; the running process used
  `/home/dev/memory-mcp/memory.db`.

Import:

```text
source head: bc49291654c6509d446d5a96d497db1cb4b5e9c8
journal commit: 9e8cdf25239cec5483b68780b120acf5bd9de42b
memory facts: 232
```

`panelmem-kb` HEAD was checked before and after import and stayed
`bc49291654c6509d446d5a96d497db1cb4b5e9c8`.

The installed unit was updated from `/home/dev/memory-mcp/memory-mcp.service`
and restarted. The live process env after restart included:

```text
MEMORY_CANON_ROOT=/home/dev/secretary-data/memory/facts
MEMORY_CANON_EXPORT=/home/dev/secretary-data/memory/export.ndjson
MEMORY_DB=/home/dev/secretary-data/memory/index.sqlite
```

Readiness gate was not systemd `active`; it waited for `secretary memory verify`
and `memory_search`. During model warmup, `memory_search` returned
`status=not_ready`, `error=embedder_loading`, not a transport failure. After the
initial rebuild:

```text
journal commit: 9e8cdf25239cec5483b68780b120acf5bd9de42b
memory facts: 232
export facts: 232
index facts: 232
journal dirty: no
status: ok
```

`memory_search("Phase 4 memory plane cutover rollback acceptance",
scope="project:secretary", caller="worker")` returned the imported Phase 4
contract fact.

### Protocol write proof

A new durable fact was written only through the protocol:

```text
fact: secretary/secretary-431-live-cutover-proof
proposal: 7d5f23cd575149b6a56547bb1c5bfc43
journal commit: 856c9b3c7a622b71ea4ad58235aa0c6397a67a86
source: secretary:worker/431
```

After watcher rebuild:

```text
journal commit: 856c9b3c7a622b71ea4ad58235aa0c6397a67a86
memory facts: 233
export facts: 233
index facts: 233
journal dirty: no
status: ok
```

Search checks:

- `memory_search(..., scope="project:secretary", caller="worker")` returned the
  proof fact at top hit, score `0.9376`.
- `memory_search(..., caller="reviewer")` returned the proof fact at top hit,
  score `0.9313`.

### Old writer sunset

`panelmem-kb` was retained as the fallback archive, not deleted. The old push
path was disabled and the tree was made readonly:

```text
origin push URL: DISABLED-readonly-after-secretary-431
/home/dev/panelmem-kb mode: 555
/home/dev/panelmem-kb/.git mode: 555
/home/dev/panelmem-kb/memory mode: 555
```

A direct write probe under `/home/dev/panelmem-kb/memory` failed with
`Permission denied`.

Curator deterministic precheck was run once after readonly. It exited `0`, and
`panelmem-kb` HEAD stayed
`bc49291654c6509d446d5a96d497db1cb4b5e9c8` before and after the run. No
`panelmem-kb` write or push occurred in that check.

### Closure state

Phase 4 live gates are green in the secretary appliance record above. The
control-panel design/backlog text still contains older Phase 4 wording and is
outside the `secretary` repository branch for this card; update it from this
record after merging the secretary PR.

## Suggested Design Doc Edits

- Fix the memory layout wording in `design-secretary-appliance.md`. It shows
  `facts/` containing `global/` and `project/`. The real layout, matching
  `memory-mcp.scope_for` and the exporter, is one directory per scope
  (`global` plus a dir per project). There is no `project/` intermediate level.
- State that the git journal root is `memory/facts`, with `export.ndjson`,
  `index.sqlite` and `manifest.json` as derived siblings outside the repo.
- Record the commit-identity rule: fixed service identity for git author, acting
  principal in the commit message.
- Name the `memory-mcp-404` readiness fix as a Phase 4 cutover dependency, not a
  later optimization, since a cold model load must not 404 the freshly cut-over
  service.
- Make explicit that Phase 4 marks `panelmem-kb` readonly and disables the old
  curator push, while the `doctor` guard against direct writes to the old repo is
  Phase 9.
