# Phase 3 Review

Phase 3 goal: create a live `secretary-data` backup of the current system,
verify the archive, and compare the archive contents with the Phase 3
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

The original live backup was created before the transcript policy and
claimed-worker guard fixes. That archive is retained as incident context, but it
is not the acceptance artifact for this review because current `backup verify`
correctly rejects its transcript payload copies.

The current-code live backup was run by the secretary/operator outside the
claimed worker context on 2026-07-10, using the live board environment from
`/home/dev/control-panel/.env`:

```bash
python3 -m secretary backup create --instance /home/dev/secretary-instance
```

Output:

```text
archive: /home/dev/secretary-data/backups/secretary-backup-20260710T150552Z.tar.age
version: 1
status: ok
exit=0
```

The pipeline pause state after the operator run:

```json
{
  "paused": false,
  "live_state_path": "/home/dev/orca/workspaces/triggered-agents/pipeline/state/pipeline/pause.json",
  "other_pause_files": [],
  "warnings": []
}
```

Verified the encrypted archive with the current verifier:

```bash
python3 -m secretary backup verify \
  /home/dev/secretary-data/backups/secretary-backup-20260710T150552Z.tar.age \
  --age-identity /home/dev/.config/secretary/age/keys.txt
```

Output:

```text
archive: /home/dev/secretary-data/backups/secretary-backup-20260710T150552Z.tar.age
version: 1
status: ok
exit=0
```

Local product tests:

```bash
python3 -m unittest
```

Result from the first review pass: 127 tests passed.

After the reviewer return, the backup command was changed so transcript payload
copies are opt-in, claimed worker contexts are refused, concurrent create runs
are locked, and preexisting pipeline pauses are rejected. The focused regression
test was rerun:

```bash
python3 -m unittest tests.test_backup
```

Result: 15 tests passed.

## Archive Contents

Verified by decrypting
`/home/dev/secretary-data/backups/secretary-backup-20260710T150552Z.tar.age`
into a temporary directory and inspecting the tar members without extracting
into any product or instance path.

Versions manifest:

```text
created_at: 2026-07-10T15:05:52+00:00
git_commit: c42376a6eedbe35e85a171d87dd6782dd76b2024
members: 326
```

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
board cards: 167
raw active task count: 167
memory facts: 164
run records: 5580
transcript inventory entries: 2980
artifacts: 4
```

The normalized board export recorded `raw_active_task_count: 167`, matching the
board component count at snapshot time. A fresh live `pipeline list` also
returned 167 cards during this review update. The current `panelmem-kb` live
source has 164 markdown facts under `/home/dev/panelmem-kb/memory`, matching the
archive memory export count.

Forbidden entry checks:

```text
project-local .env entries: none
actual Orca state entries: none
Orca debug snapshot: secretary-backup/debug/orca-state/inventory.json
nested secretary-data/backups entries: none
generated systemd unit entries: none
transcript payload copies: none
```

The archive contains only the debug Orca inventory JSON, not the state files
themselves.

Earlier incident archives at 12:55Z, 14:20Z, 14:22Z, 14:25Z, and 14:28Z were
created by code before the transcript policy fix, or by the claimed-worker
self-kill incident tracked as `secretary-379`. They contain transcript payload
copies and current `backup verify` rejects them. They are not the Phase 3
acceptance artifact.

## Acceptance Check

`secretary backup create` makes a consistent archive, with a short pipeline
pause allowed.

Status: done.

The current-code operator run created
`/home/dev/secretary-data/backups/secretary-backup-20260710T150552Z.tar.age`.
`pause-status` was clear after the command. The command itself uses
`pipeline pause freeze` and `resume` internally.

`secretary backup verify` checks structure and versions.

Status: done.

Current `backup verify` returned exit code 0 and printed `version: 1` and
`status: ok` for the acceptance archive.

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

The archive board component has 167 cards and its raw Kanboard active task count
is 167. A fresh live query during this review update also returned 167 cards.
The memory export count is 164 and matches the current live `panelmem-kb`
markdown fact count.

No host changes beyond normal backup work.

Status: mixed.

The review did not edit `/home/dev/secretary-instance` or host configs. The
normal backup work updated generated files under `/home/dev/secretary-data` and
created the encrypted acceptance archive.

The earlier claimed-worker attempts were not normal backup work: they entered a
pause/resume relaunch path, created extra backup artifacts, and restarted or
froze neighboring work. That incident is tracked separately as `secretary-379`.
Current `backup create` refuses `BOARD_ROLE=worker` contexts to prevent this
path.

## Real Gaps

The live backup command needs the Kanboard environment from
`/home/dev/control-panel/.env`, but the design doc command is shown as a bare
`secretary backup create`. Without that environment, the command fails before
snapshotting.

Running `backup create` from a claimed worker is unsafe with the current
pipeline resume behavior. During this review flow, card `secretary-379` was
created for the defect: hard resume can relaunch in-flight workers, including
the worker that invoked backup. The product now has a guard that refuses
`BOARD_ROLE=worker` contexts.

The first live archive copied full transcript JSONL payloads even though Phase 3
calls for transcript inventory. The product now makes payload copies opt-in via
`--copy-transcripts`, and `backup verify` reports `transcripts/copies` entries
as unexpected. The current acceptance archive has no transcript payload copies.

Concurrent `backup create` runs could previously share a pause boundary and
mutate the same generated snapshot files. The product now takes an advisory
backup create lock and refuses to run under a preexisting pause, so each archive
owns the freeze that protects its snapshot.

Offsite pull is still unconfirmed. The archive exists on the VPS, but
`last_fetch` is absent, so the backup does not yet satisfy the offsite
protection part of the design.

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
a claimed worker task. The current product guard refuses the claimed-worker
environment, but the design doc should still make the operating context
explicit.

Document the transcript boundary: Phase 3 backups include transcript inventory
only by default. Copying transcript bodies requires explicit operator policy and
`--copy-transcripts`.

Split Phase 3 acceptance into two checks: VPS-side backup create and verify, and
offsite pull from the local computer. The second check needs vladmesh or an
operator with local-machine access.

Clarify the count comparison boundary. The archive should prove internal
snapshot consistency, for example normalized board count equals raw Kanboard
active task count, while a later live board count may differ if new cards appear
after the snapshot.
