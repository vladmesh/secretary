# Live cutover journal

Updated: 2026-07-17 04:58 Europe/Vilnius

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

Status: pending.

- Stop the old memory owner, install/start the product memory unit, then run a real memory search
  and parity verification.
- Replace the installed backup unit so it no longer references `control-panel/.env`; keep the timer
  schedule and verify the rendered command.
- On failure, stop the new unit and restart the old unit before continuing.

### 5. Pilot the product dispatcher

Status: pending.

- Select one explicit pilot card.
- Put the legacy dispatcher into a human hard freeze with no auto-resume.
- Run product preflight and the full worker-to-reviewer pilot lifecycle.
- Roll back on any ownership, claim, terminal, board audit or review-state divergence.

### 6. Commit production ownership

Status: pending.

- Commit cutover only after the pilot is green.
- Enable the product production dispatcher/timer and verify exactly one owner.
- Run live doctor, memory verify, backup smoke and board read/write smoke.

### 7. Rollback window and decommission

Status: pending.

- Keep legacy checkouts disabled but present for a short observation window.
- Remove legacy services, timers and nonessential Orca registrations only after the checks remain
  green.
- Preserve `/home/dev/control-panel` until this Codex session has moved or ended.
- Delete legacy checkouts last, then rerun doctor and backup verification.

## Checkpoint log

- 2026-07-17 04:49: journal created. No live owner or service changed.
- 2026-07-17 04:51: step 1 completed. Product, instance and this recovery journal pushed to GitHub.
- 2026-07-17 04:54: step 2 completed. Fresh core/full archives created and strict-verified; pipeline
  resumed with no in-progress cards.
- 2026-07-17 04:58: step 3 completed. Runtime env materialized, units verified, and the two product
  Orca registrations explicitly adopted. Live owners unchanged.
