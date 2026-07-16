# secretary

Product repository for the portable secretary appliance.

Phase 7 adds a read-only host plan and inventory. It still does not apply host
changes, own live board data, secrets, transcripts, or instance-specific paths.

## Config schemas

The contract between the product and a future `secretary-instance` repo lives in
`secretary/schemas/` as JSON Schema (draft 2020-12):

- `instance.schema.json` — top-level `instance.yaml`
- `project-binding.schema.json` — one connected project under `projects/`
- `adapter.schema.json` — a project adapter under `adapters/`
- `data-manifest.schema.json` — the `secretary-data` layout descriptor

`examples/instance/` is a complete, valid instance tree with placeholder-only data
(no live bindings). `doctor --offline` and the tests validate against it.

## CLI

Install dependencies and run the tests:

```bash
python3 -m pip install .
python3 -m unittest
```

Run the read-only doctor against the example instance without accessing the host:

```bash
python3 -m secretary doctor --offline --instance examples/instance
```

`doctor` validates `instance.yaml` plus every binding, adapter and the data
manifest it finds. By default it also reads the live host inventory; `--offline`
skips that inventory. The canonical manifest location is
`<data_dir>/data-manifest.json`; the old example-local `data-manifest.json` is still
accepted for fixture compatibility. A missing manifest is a migration warning, so
plain `doctor --offline` stays green and `--strict` returns non-zero. `--instance`
accepts an instance directory or a direct path to an `instance.yaml`. An invalid
config prints one problem per line with a path to the offending field, and exits
non-zero:

```text
secretary doctor: 1 config problem(s):
  example-project.yaml: id: 'Bad_Id' does not match '^[a-z0-9][a-z0-9-]*$'
```

## Host inventory

Phase 7 renders a plan before it can change a host:

```bash
python3 -m secretary reconcile plan --instance ~/secretary-instance \
  --managed-manifest /path/to/host-managed.json
```

The plan is read-only and deterministic. Heads render supported systemd services;
enabled project bindings render Orca registrations. A binding supplies its repo path
and `orca_binding` explicitly. Neither name is derived from a project id. The managed
manifest records the logical resource id, kind, name and fingerprint after a future
apply. A host name match with no matching managed record is a `conflict`, not a right
to change it. This card deliberately does not implement `reconcile --apply`.

The plan rejects incomplete desired inputs: heads require `host.unit_prefix`, and an
enabled binding requires an explicit `orca_binding`. This validation is plan-local so
`doctor --offline` can still inspect a pre-migration instance without changing it.
Logical resource ids and host names must also be unique within a plan.

By default `reconcile plan` reads the same live, read-only inventory boundary as
`doctor`: project directories, `systemctl list-unit-files`, and `orca repo list`.
Use `--host-fixture DIR` for deterministic tests or offline checks. `--offline`
cannot produce a plan because a plan needs inventory; use the fixture override
without `--offline`. The plan exits 0 when inventory was read and there are no conflicts, 1
for conflicts, and 2 for invalid input or an unavailable inventory kind. It never
writes the managed manifest or applies host changes; `reconcile --apply` remains
out of scope.

An existing desired Orca registration remains a conflict until an operator verifies
and adopts it one resource at a time:

```bash
python3 -m secretary reconcile adopt --instance ~/secretary-instance \
  --logical-id orca:project:secretary
# inspect the fingerprint, then repeat with --yes
```

Adopt compares both the registration name and its normalized live repo path with the
explicit binding. Without `--yes` it is preview-only. With confirmation it atomically
writes only `host-managed.json`; it does not change Orca, systemd or worktrees. The
command rejects corrupt, duplicate, drifted or symlinked state rather than replacing it.
Rollback is restoring the previous manifest from backup before any later apply step.

`doctor` checks the live host by default and never writes. Use `--offline` for
config/data-only checks. Its exit codes are 0 for a completed clean check, 1 for
findings (and warnings with `--strict`), and 2 when config or inventory cannot be
checked.

## Dispatcher pilot

Phase 7 includes a product-owned pilot dispatcher:

```bash
python3 -m secretary dispatcher preflight --instance ~/secretary-instance \
  --pilot-ref secretary-000
python3 -m secretary dispatcher pause-old --instance ~/secretary-instance \
  --pilot-ref secretary-000 --evidence-file /tmp/old-dispatcher-paused.txt
python3 -m secretary dispatcher start-new-pilot --instance ~/secretary-instance \
  --pilot-ref secretary-000
python3 -m secretary dispatcher tick --instance ~/secretary-instance \
  --pilot-ref secretary-000
```

The pilot dispatcher fails closed without an exact `--pilot-ref` and matching
cutover state. `preflight` and `start-new-pilot` also require the live legacy
pause state to be `freeze`; `drain` is blocked because the legacy watchdog and
advance path remain active. It writes only through `secretary task`, including
dispatcher claim, worker report handling, Validate moves, reviewer verdict
handling and terminal transitions. Rollback stops the new host handles and
leaves the board card, claim, comments, PR and review state intact for the old
dispatcher.

See `docs/dispatcher-cutover.md` for the full operator flow and the remaining
post-merge live pilot condition before production ownership moves from the
legacy dispatcher.

## Task creation

`secretary task create` writes a Pipeline card through the task protocol and
records the creation in `secretary-data/board/events.ndjson`.

```bash
python3 -m secretary task create --role po --project secretary --type code \
  --title "Codex exec worker" --state ready --head codex-extra \
  --codex-mode exec

python3 -m secretary task create --role po --project secretary --type code \
  --title "Codex TUI worker" --state ready --head codex-extra \
  --codex-mode tui
```

`--codex-mode` is accepted only when the worker head profile is a Codex profile.
If it is omitted, the dispatcher uses the head profile's `codex_mode`; profiles
without that field launch through the safe `exec` path.

The default doctor inventory is read-only. It compares project repos, systemd units
and Orca repo registrations and prints three sets:

- `matched` — described in the instance and present on the host;
- `missing-on-host` — described in the instance but not found;
- `unmanaged-on-host` — present on the host but not described (reconcile would
  leave these alone).

The `host` block supplies `projects_root` and `unit_prefix` ownership boundaries.
The plan does not read `host.units` or `host.orca_repos`; they remain deprecated
doctor compatibility inputs, not desired state. Expected project names come from
bindings under `projects/`.

The inventory is strictly read-only: it enumerates resource names only and never
opens env files, reads secrets, or changes host state. `--host-fixture DIR` runs the
same comparison against a fixture host directory instead of the live host, so it can
run offline and under test:

```bash
python3 -m secretary doctor --instance examples/instance \
  --host-fixture tests/fixtures/host
```

Use `--offline` when an inventory source must not be accessed.

## Data exports

Phase 3 adds exporters for the current system:

```bash
python3 -m secretary data export --instance ~/secretary-instance
```

The combined export writes normalized board cards, memory facts, pipeline run
state and transcript inventory under `secretary-data/`. Narrow commands are also
available for one component at a time: `export-board`, `export-memory`,
`export-runs` and `export-transcripts`. Memory export seeds or syncs the local
`memory/facts` git journal from `panelmem-kb`, then writes derived
`memory/export.ndjson` and `memory/manifest.json` next to it. Re-running it does
not create a new import commit when facts are unchanged.

The explicit memory import command is available for the pre-cutover sync:

```bash
python3 -m secretary memory import --instance ~/secretary-instance \
  --from ~/panelmem-kb
```

`panelmem-kb` remains readable and unchanged. It is retained as the rollback
source until cutover moves writers to the secretary memory protocol. `reconcile
plan` is available as a read-only host planner; `reconcile --apply` is not part of
this phase. `backup`, `restore`, `project add`, `task`, and public memory writer
commands retain their individual command contracts.

## Data layout

Create the target data directory and manifest from an instance:

```bash
python3 -m secretary data init --instance /home/dev/secretary-instance
```

This creates `board/`, `memory/`, `runs/`, `transcripts/`, `artifacts/`,
`backups/`, initializes `memory/facts` as a local git repository without a
remote, and writes `data-manifest.json` in the data directory. The command is
idempotent and overwrites only the generated manifest.

Capture a raw Kanboard storage dump into the board data layer:

```bash
python3 -m secretary data raw-kanboard-dump --instance /home/dev/secretary-instance
```

The dump uses `docker cp cp-kanboard:/var/www/app/data` and writes a new
`board/kanboard-raw-<timestamp>/` directory each time. It does not call the
Kanboard API and does not write into the live container.

## Offsite pull

Backups are protected only after an offsite machine pulls the encrypted archives
from the VPS. Run this on the local machine:

```bash
scripts/pull-backups-offsite.sh vps.example.com /home/dev/secretary-data ~/secretary-backups
```

The script copies `secretary-data/backups/*.tar.age` over ssh with `rsync`, falling
back to `scp` when `rsync` is unavailable. After a successful pull it atomically
updates `secretary-data/backups/last_fetch` on the VPS. `doctor` reads
`offsite.backup_pull_max_age_days` from `instance.yaml`: a missing `last_fetch`
is a warning, while a stale marker is a finding and exits non-zero.

## Backup policy

`secretary backup create --kind both` creates one core archive and one full archive
under `secretary-data/backups/` during a single pipeline pause. Core archives contain
the active normalized board export without Done cards, full memory export, run
watermarks, card mapping, claims, instance config and the versions manifest. Full
archives contain the raw board dump, full normalized export, runs, transcript
inventory, artifacts and debug inventory.

After a successful create, VPS retention keeps only the latest core archive and
removes full archives older than 48 hours. The offsite pull script still copies
all `*.tar.age` archives it sees and never deletes local files, so the local
machine keeps the point-in-time series.

Daily operator timer templates live in `docs/systemd/`. The timer runs around
04:00 UTC and calls `backup create --kind both`, so core and full are captured
with one pause. Install and enable those templates only after the deployed
`/home/dev/secretary` checkout contains the merged code; ephemeral worker
workspaces should not install live units.

## Documentation

- `docs/target-layout.md` describes the target `secretary`, `secretary-instance`
  and `secretary-data` split, plus the purpose of each target command.
- `docs/phase1-review.md` records the Phase 1 acceptance check and differences
  between the current skeleton and the design doc.
- `docs/restore-contract.md` describes `bootstrap --empty`, `restore`, their
  handoffs, and how to reproduce the restore chain locally.
- `docs/phase8-review.md` maps the restore implementation onto the Phase 8
  acceptance and records the remaining operator off-host run.
