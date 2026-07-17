# Live cutover journal

Updated: 2026-07-17 05:14 Europe/Vilnius

Status: preparation started. Production ownership has not changed.

## Goal

Make `secretary` and `secretary-instance` the only repositories required by the live secretary
installation. Mutable board, memory, runs, transcripts and backups remain in `secretary-data`.
After a verified rollback window, remove the host dependencies on `control-panel`, `memory-mcp`,
`panelmem-kb` and `triggered-agents`.

This is an in-place cutover. The existing board and memory facts are retained. It is not a restore
and it must not recreate project data from an archive unless rollback becomes necessary.

## Why

Product code, configuration and runtime ownership are currently split across legacy repositories.
That makes backup, restore, upgrades and development depend on historical checkout paths. The code
has already been absorbed side-by-side; this procedure changes the live owners and then removes the
obsolete dependencies.

## Recovery rule

Update this file after every completed step, before starting the next one. Commit and push useful
checkpoints before any operation that can stop the current terminal or remove a checkout. If the
operator session disappears, resume by reading this file, `OPERATIONS.md`, the latest verified
backup names below and the current systemd/Orca inventory. Do not infer completion from a planned
step.

The current Codex session runs from `/home/dev/control-panel`. Do not remove that checkout or its
Orca registration while this session is alive. Decommission it only from a replacement secretary
terminal or after this session has ended.

## Baseline

- `secretary` code and consolidated documentation are committed locally through `ef96fa0`.
- `secretary-instance` context is committed locally through `c2ab05b`.
- Product tests: 410 passed.
- `doctor --offline`: green.
- live `doctor`: green, read-only.
- memory parity: 233 journal, 233 export, 233 index; journal clean.
- verified core backup: `secretary-backup-core-20260717T005057Z.tar.age`.
- verified full backup: `secretary-backup-full-20260717T005057Z.tar.age`.
- `memory-mcp.service` is still active from `/home/dev/memory-mcp/run.sh`.
- installed `secretary-backup.service` still reads `/home/dev/control-panel/.env`.
- `secretary-memory.service` is inactive.
- `reconcile plan` reports conflicts for existing legacy Orca registrations and backup units; the
  future production dispatcher units are planned as creates.
- No production dispatcher owner is recorded by the product. Product state is pilot-only.

## Plan and checkpoints

### 1. Publish recoverable documentation and code

Status: completed.

- Push `secretary` and `secretary-instance` commits without rewriting history.
- Confirm both remotes contain this journal and the consolidated documentation.
- Do not mix the divergent codegen checkout into the cutover.

Result: `secretary` through `b4dad69` and `secretary-instance` through `c2ab05b` are published on
their `origin/main` branches. Both local branches match their remotes. The codegen checkout was not
changed or pushed during this step.

### 2. Capture a cutover backup

Status: completed.

- Create fresh core and full encrypted archives using the current live contour.
- Strict-verify both with the configured age identity.
- Record exact archive names here.

Result: backup create completed and released the pipeline pause. `pipeline pause-status` is false
and no cards are in `In progress`. Strict verification passed for:

- `secretary-backup-core-20260717T015035Z.tar.age`
- `secretary-backup-full-20260717T015035Z.tar.age`

### 3. Prepare new runtime without changing owners

Status: completed.

- Verify `/home/dev/secretary/.venv` and the memory extra.
- Render and inspect the product systemd units against `secretary-instance/runtime.env`.
- Resolve ownership conflicts explicitly. Never adopt a resource by name alone.
- Keep old services running during preparation.

Result:

- `/home/dev/secretary/.venv` imports both product and compatibility packages.
- `/home/dev/secretary-instance/runtime.env` was materialized from the current live env with mode
  `0600`; it is ignored by Git through `secretary-instance/.gitignore`.
- all product service/timer assets passed `systemd-analyze verify`.
- existing Orca registrations for `secretary` and `secretary-instance` were explicitly matched by
  name and normalized repo path and recorded in `secretary-data/host-managed.json`.
- remaining reconcile conflicts are intentionally not adopted: legacy `control-panel` and
  `triggered-agents`, unrelated existing `public_profile` and `vladmesh`, and the old backup units.
  The old backup units must be replaced during step 4; legacy registrations are removed only in
  step 7. Production dispatcher units remain planned creates.
- old memory and backup owners remained active during this step.

### 4. Switch memory and backup owners

Status: completed.

- Stop the old memory owner, install/start the product memory unit, then run a real memory search
  and parity verification.
- Replace the installed backup unit so it no longer references `control-panel/.env`; keep the timer
  schedule and verify the rendered command.
- On failure, stop the new unit and restart the old unit before continuing.

Result:

- installed and enabled `secretary-memory.service`; disabled and stopped `memory-mcp.service`.
- the new memory daemon reached ready state on `127.0.0.1:8077/mcp`; a real MCP `memory_search`
  succeeded and `memory verify` reports 233 journal/export/index facts with a clean journal.
- startup took about six minutes and peaked near 1.9 GiB RSS while loading the local embedding
  model. The current bootstrap uses `update_index()`: unchanged fact embeddings are reused; the
  delay was model loading, not a forced full fact rebuild.
- replaced the installed `secretary-backup.service` and timer with product assets. The service now
  reads `/home/dev/secretary-instance/runtime.env` and executes the product venv, with no
  `control-panel` path.
- a manual systemd backup smoke exited successfully and created core/full archives
  `secretary-backup-{core,full}-20260717T020228Z.tar.age`.
- old memory checkout remains present for rollback, but owns no enabled or active service.

### 5. Pilot the product dispatcher

Status: completed.

- Select one explicit pilot card.
- Put the legacy dispatcher into a human hard freeze with no auto-resume.
- Run product preflight and the full worker-to-reviewer pilot lifecycle.
- Roll back on any ownership, claim, terminal, board audit or review-state divergence.

Current checkpoint:

- created exact pilot card `secretary-621`, "Guard incremental memory bootstrap semantics";
- legacy pipeline is in manual freeze owned by `secretary-cutover`, with auto-resume ineligible and
  no stopped heads;
- the completed historical `secretary-513` pilot state was explicitly rolled back without
  committing production ownership;
- new attempt `attempt-20260717T020548Z-855b2902bba5` is in `new_pilot` phase;
- product dispatcher claimed `secretary-621` as worker `secretary-621-memory-bootstrap-guard` in
  `/home/dev/orca/workspaces/secretary/secretary-621-memory-bootstrap-guard`;
- last observe: card `in_progress`, old owner paused, no divergences. Worker/reviewer completion is
  not yet proven.

Live finding:

- worker completed the change and reported 411 green tests, including unskipped memory tests;
- product dispatcher moved the card to Validate and launched an independent reviewer;
- reviewer returned RED for a non-hermetic readiness assertion, and the dispatcher moved the card
  back to `In progress`;
- the rework lifecycle then stalled: `_advance_review` stored the record as `claimed`, while the
  original worker process had exited. Subsequent ticks return `waiting-worker-report` without
  launching a replacement worker. This is a product dispatcher bug, not a card implementation
  failure.
- production ownership has not been committed. The attempt must be rolled back, the rework launch
  fixed and covered by a test, then a fresh pilot must complete before step 5 can be marked done.

Rework checkpoint:

- fixed the dispatcher RED path so it stops the reviewer and relaunches a worker in the existing
  workspace; added a focused regression test and ran the full 411-test suite;
- fixed the pilot test on `pipeline/secretary-621`, verified it with a clean `HOME`, merged current
  `main` into the branch and pushed it without rewriting history;
- a first fresh attempt exposed a stale Orca-worktree collision and was rolled back without
  committing ownership; both stale pilot worktrees were removed;
- fresh attempt `attempt-20260717T022301Z-779138c5d504` now owns the card and recreated the expected
  workspace. Worker reported focused 3/3 and full 412/412 tests green. The dispatcher consumed the
  report, moved the card to Validate and launched the independent reviewer. Reviewer returned GREEN
  with no findings and the dispatcher moved the card to Done. Legacy remains frozen and the last
  ownership observation has no divergences.

Second pilot finding:

- the worker protocol left the reviewed diff uncommitted, but the dispatcher still accepted GREEN
  and moved the card to Done. This is a pipeline correctness bug: Done did not imply a durable Git
  result;
- the exact reviewed diff was committed manually and cherry-picked to `main` as `18628f3`; focused
  3/3 and full 412/412 tests passed again on `main`;
- fixed the contract in `b7686bc`: worker prompts now require a commit, and the dispatcher checks
  `git status --porcelain` before accepting `report:done`. A dirty or missing workspace is moved to
  Blocked before review instead of reaching Validate or Done;
- added the dirty-result regression test; focused lifecycle tests and the full 413-test suite pass.
  Production ownership remains uncommitted pending the final cutover checks.

### 6. Commit production ownership

Status: completed.

- Commit cutover only after the pilot is green.
- Enable the product production dispatcher/timer and verify exactly one owner.
- Run live doctor, memory verify, backup smoke and board read/write smoke.

Current checkpoint:

- committed dispatcher ownership after the pilot reached Done and both discovered pipeline bugs
  were fixed and covered by tests;
- state phase is `cutover_committed`, records are empty, legacy remains frozen and observe reports no
  divergences;
- production dispatcher units are not installed yet. If this session stops here, install them from
  the product packaging before unfreezing or deleting any legacy runtime.

Production unit finding:

- the first installed timer service used `production-run`, an intentionally persistent loop, from
  a oneshot timer. Its first tick completed cleanly but the process could not exit;
- disabled the timer and stopped only the new service. Legacy stayed frozen and no duplicate claim
  occurred;
- fixed the packaged unit and desired-host runtime to use one-shot `production-tick` in `62431ac`.
  Host tests and the full 413-test suite pass. Reinstall the corrected unit before continuing.

Result:

- corrected production service and timer are installed, enabled and active. A manual service start
  completed in about five seconds with `production-tick` status ok; the timer is waiting;
- production unit ownership is recorded in `host-managed.json`; live doctor reports status ok;
- memory parity is 233 journal/export/index facts with a clean journal;
- latest core and full archives pass strict verification;
- board write/read smoke succeeded by appending and reading a dispatcher comment on `secretary-621`;
- legacy dispatcher remains frozen. It can now be removed instead of resumed.

### 7. Rollback window and decommission

Status: completed except for the current terminal lifetime.

- Keep legacy checkouts disabled but present for a short observation window.
- Remove legacy services, timers and nonessential Orca registrations only after the checks remain
  green.
- Preserve `/home/dev/control-panel` until this Codex session has moved or ended.
- Delete legacy checkouts last, then rerun doctor and backup verification.

Current checkpoint:

- disabled the last enabled legacy timers `ta-pipeline.timer` and `ta-retro.timer`; all ten `ta-*`
  unit files are now disabled/static and no legacy service is running;
- closed idle secretary curator/retro/steward shells but kept their permanent worktrees for future
  product automations;
- removed the temporary `secretary-621` pilot worktree. Orca now has only this current control-panel
  terminal open;
- the completed codegen megatest process and terminal were already absent at inspection time.

Result:

- removed all legacy `ta-*` unit files and the disabled `memory-mcp.service`; daemon ownership stays
  with `secretary-memory.service`;
- added explicit `decommission-old` state in `2144293`, so doctor distinguishes an absent legacy
  owner from a missing freeze fence. Full suite is green at 414 tests;
- recorded the live decommission fence and doctor returned status ok;
- removed and purged `triggered-agents`, `memory-mcp` and `panelmem-kb`, freeing about 293 MiB. Orca
  pruned their stale registrations after the paths disappeared;
- `/home/dev/control-panel` is removed by the final command after this checkpoint is pushed. Its
  current terminal may remain visible until this Codex session ends, but no legacy runtime owns
  services, timers, memory, backups or task dispatch.

## Checkpoint log

- 2026-07-17 04:49: journal created. No live owner or service changed.
- 2026-07-17 04:51: step 1 completed. Product, instance and this recovery journal pushed to GitHub.
- 2026-07-17 04:54: step 2 completed. Fresh core/full archives created and strict-verified; pipeline
  resumed with no in-progress cards.
- 2026-07-17 04:58: step 3 completed. Runtime env materialized, units verified, and the two product
  Orca registrations explicitly adopted. Live owners unchanged.
- 2026-07-17 05:04: step 4 completed. Memory and backup owners switched to product units; real MCP,
  memory parity and systemd backup smoke are green.
- 2026-07-17 05:07: step 5 started. `secretary-621` claimed by fresh product pilot attempt; observe
  is clean and legacy remains frozen.
- 2026-07-17 05:14: pilot exercised worker and reviewer paths, then found a rework relaunch bug after
  RED. Production commit stopped; fix and fresh pilot required.
- 2026-07-17 05:23: dispatcher rework relaunch fixed and tested; pilot branch made hermetic. Stale
  worktrees from the failed restart were removed. Fresh attempt `attempt-20260717T022301Z-779138c5d504`
  is active with clean ownership observation.
- 2026-07-17 05:37: fresh worker reported 3 focused and 412 full tests green. Product dispatcher
  moved `secretary-621` to Validate and launched the independent reviewer; production ownership is
  still uncommitted.
- 2026-07-17 05:44: reviewer returned GREEN and the pilot reached Done, exposing that the dispatcher
  accepted an uncommitted reviewed diff. The diff was preserved on `main` as `18628f3` and all 412
  tests passed again. Production commit remains stopped pending a Git durability gate.
- 2026-07-17 05:47: Git durability gate added as `b7686bc`; dirty done reports now block before
  review. Full suite is green at 413 tests. Pilot findings are resolved in product code.
- 2026-07-17 05:51: logical dispatcher ownership committed. State is `cutover_committed`, records are
  empty and observe is clean. Legacy remains frozen until product production units pass live checks.
- 2026-07-17 05:53: first production timer smoke exposed a persistent-loop/oneshot mismatch. New
  timer was disabled and service stopped; corrected to `production-tick` in `62431ac`, 413 tests
  green. No legacy or task owner was resumed.
- 2026-07-17 05:56: corrected production timer active and waiting; doctor, memory parity, strict
  core/full backup verification and board write/read smoke are green. Step 6 complete; legacy can
  be decommissioned without resume.
- 2026-07-17 05:57: legacy `ta-*` timers disabled, idle shells closed and pilot worktree removed.
  Only the current control-panel terminal remains open. Permanent new secretary automation
  worktrees remain registered but idle.
- 2026-07-17 06:01: legacy decommission recorded, doctor green, old unit files removed and legacy
  memory/agent repositories purged. Final control-panel deletion scheduled immediately after this
  pushed checkpoint.
