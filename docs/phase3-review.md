# Phase 3 Review

Phase 3 goal: create the first live `secretary-data` backup of the current
system, verify the archive, and compare the archive contents with the Phase 3
acceptance boundary.

## Commands Run

Checked the pipeline pause state before the live backup:

```bash
PYTHONPATH=/home/dev/orca/workspaces/triggered-agents/pipeline \
  python3 -m triggered_agents pipeline --role steward pause-status
```

Output:

```json
{
  "paused": false,
  "live_state_path": "/home/dev/orca/workspaces/triggered-agents/pipeline/state/pipeline/pause.json",
  "other_pause_files": [],
  "warnings": []
}
```

The first `backup create` attempt was run without the Kanboard environment and
failed before creating an archive:

```bash
python3 -m secretary backup create --instance /home/dev/secretary-instance
```

Output:

```text
secretary backup create: pipeline: missing KANBOARD_URL in env (source control-panel/.env before running)
```

The pause state was checked again and was still clear:

```json
{
  "paused": false,
  "live_state_path": "/home/dev/orca/workspaces/triggered-agents/pipeline/state/pipeline/pause.json",
  "other_pause_files": [],
  "warnings": []
}
```

The successful live backup used the live board environment from
`/home/dev/control-panel/.env`:

```bash
set -a
. /home/dev/control-panel/.env
set +a
python3 -m secretary backup create --instance /home/dev/secretary-instance
```

Output:

```text
archive: /home/dev/secretary-data/backups/secretary-backup-20260710T142833Z.tar.age
version: 1
status: ok
```

The pipeline pause state after the command:

```json
{
  "paused": false,
  "live_state_path": "/home/dev/orca/workspaces/triggered-agents/pipeline/state/pipeline/pause.json",
  "other_pause_files": [],
  "warnings": []
}
```

Verified the encrypted archive:

```bash
python3 -m secretary backup verify \
  /home/dev/secretary-data/backups/secretary-backup-20260710T142833Z.tar.age \
  --age-identity /home/dev/.config/secretary/age/keys.txt
```

Output:

```text
archive: /home/dev/secretary-data/backups/secretary-backup-20260710T142833Z.tar.age
version: 1
status: ok
```

Exit code: 0.

Local product tests:

```bash
python3 -m unittest
```

Result: 127 tests passed.

## Archive Contents

Verified by decrypting the archive into a temporary directory and inspecting the
tar members without extracting into any product or instance path.

Versions manifest components:

```text
artifacts
board
debug_orca_state
memory
raw_board
runs
transcripts
```

Counts recorded in the archive:

```text
board cards: 166
memory facts: 163
run records: 5485
transcripts: 2968
artifacts: 5
```

The normalized board export also recorded `raw_active_task_count: 166`, so the
normalized export matched the raw Kanboard active task count at snapshot time.
After the backup finished, a fresh live `pipeline list` returned 167 cards. The
extra live card was `secretary-379`, created after the snapshot, so this is live
board movement after the archive boundary rather than a backup mismatch.

The current `panelmem-kb` live source has 163 markdown facts under
`/home/dev/panelmem-kb/memory`, matching the archive memory export count.

Forbidden entry checks:

```text
project-local .env entries: none
actual Orca state entries: none
Orca debug snapshot: secretary-backup/debug/orca-state/inventory.json
nested secretary-data/backups entries: none
generated systemd unit entries: none
```

The debug Orca inventory listed 6187 files from Orca state roots, but the archive
contains only that inventory JSON, not the state files themselves.

## Acceptance Check

`secretary backup create` makes a consistent archive, with a short pipeline
pause allowed.

Status: done for the live system.

The successful run created
`/home/dev/secretary-data/backups/secretary-backup-20260710T142833Z.tar.age`.
`pause-status` was clear before and after the command. The command itself uses
`pipeline pause freeze` and `resume` internally.

`secretary backup verify` checks structure and versions.

Status: done.

`backup verify` returned exit code 0 and printed `version: 1` and `status: ok`.

Archive is pulled to a local computer and `last_fetch` is updated.

Status: open.

`/home/dev/secretary-data/backups/last_fetch` is absent after the VPS-side
backup. A worker on the VPS cannot perform the real local-machine pull. This
needs vladmesh to run `scripts/pull-backups-offsite.sh` from the local computer
or to accept a separate open item for that manual step.

Project-local `.env` files are not included in backup.

Status: done.

The archive member list contains no path component starting with `.env`.

Orca state is saved only as a debug snapshot.

Status: done.

The archive contains `secretary-backup/debug/orca-state/inventory.json` and no
actual `.orca` or `.config/orca` state entries.

Counts in board and memory exports match live sources.

Status: done for the snapshot boundary.

The archive board export has 166 cards and its raw Kanboard active task count
was also 166. A post-backup live query showed 167 cards because `secretary-379`
appeared after the snapshot. The memory export count is 163 and matches the
current live `panelmem-kb` markdown fact count.

No host changes beyond normal backup work.

Status: done with one caveat.

The review did not edit `/home/dev/secretary-instance` or host configs. The
normal backup work updated generated files under `/home/dev/secretary-data` and
created the encrypted archive. The first failed `backup create` attempt did not
create an archive and left the pipeline unpaused.

## Real Gaps

The live backup command needs the Kanboard environment from
`/home/dev/control-panel/.env`, but the design doc command is shown as a bare
`secretary backup create`. Without that environment, the command fails before
snapshotting.

Running `backup create` from a claimed worker is unsafe with the current pipeline
resume behavior. During this review flow, a new card `secretary-379` was created
for the defect: hard resume can relaunch in-flight workers, including the worker
that invoked backup, which can cause a repeated create cycle. The Phase 3 review
still produced a valid archive, but the command contract should say who may run
the live backup or the product should add a guard.

Offsite pull is still unconfirmed. The archive exists on the VPS, but
`last_fetch` is absent, so the backup does not yet satisfy the offsite protection
part of the design.

The acceptance wording says counts should match the live board. On an active
board, the exact live count can change immediately after the snapshot. The
stronger check is that normalized export count matches raw Kanboard active task
count inside the same paused snapshot, then separately note any later live
movement.

## Suggested Design Doc Edits

Document the environment needed for live board export before `backup create`.
For the current system that means sourcing `/home/dev/control-panel/.env` or
providing the same Kanboard variables through the future instance runtime.

State that live `backup create` should be run from an operator context, not from
a claimed worker task, until the resume behavior has an explicit self-exclusion
or the product refuses unsafe invocation.

Split Phase 3 acceptance into two checks: VPS-side backup create and verify, and
offsite pull from the local computer. The second check needs vladmesh or an
operator with local-machine access.

Clarify the count comparison boundary. The archive should prove internal
snapshot consistency, for example normalized board count equals raw Kanboard
active task count, while a later live board count may differ if new cards appear
after the snapshot.
