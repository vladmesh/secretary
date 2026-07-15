# Dispatcher cutover

This runbook covers the reversible Phase 7 pilot and the later production
dispatcher cutover. The old dispatcher remains the production owner until an
operator freezes it, commits cutover state and starts the managed production
dispatcher.

The product runtime lives in `secretary.dispatcher` and the CLI entrypoint is
`secretary dispatcher`. It uses the public `secretary task` protocol for all board
writes. The live runtime does not import Python modules from `triggered-agents`.

## Pilot safety contract

- `--pilot-ref` is required for every dispatcher command. There is no broad Ready
  scan mode in the pilot runtime.
- `preflight`, `pause-old`, `start-new-pilot`, `tick` and `commit-cutover` read
  the live legacy pause file and require `freeze` (`mode: hard`). Operator
  evidence alone is not enough, and `drain` is not enough because the old
  dispatcher can still advance cards, run Validate and fire the watchdog.
- An automation-owned `freeze` is not enough while the legacy dispatcher
  auto-resume TTL is enabled. Use a human actor for the cutover window or disable
  the legacy hard-pause auto-resume TTL for that maintenance window.
- Cutover state is stored under `<data_dir>/dispatcher/pilot-state.json`.
- The new tick is serialized by `<data_dir>/dispatcher/pilot-tick.lock`.
- Each `start-new-pilot` attempt records a stable `attempt_id`. Dispatcher board
  writes include that id in their `secretary task --request-id`, so retrying one
  attempt is idempotent but a later pilot attempt cannot replay old committed
  claim events.
- Rollback stops new dispatcher terminals through the host adapter and leaves
  the card, claim, comments, PR and review state unchanged.
- Worker worktrees are landed on `pipeline/<ref>`, the same branch name the
  legacy Validate/reviewer path fetches. A rollback during Validate therefore
  resumes against the existing PR head without a manual branch rename or push.
- Claude worker/reviewer profiles are prepared before launch by setting
  `projects["<workspace>"].hasTrustDialogAccepted = true` and a top-level
  `theme` in the Claude config when it is absent. Those are the two Claude
  first-run prompts the production launcher pre-answers. The write is
  fail-closed: an unreadable, corrupt, symlinked or non-atomically writable
  config blocks the launch instead of opening an interactive folder-trust
  prompt or onboarding theme picker.
- `SECRETARY_DISPATCHER_HOST_MODE=noop` is only for tests and fixture pilots. A
  live pilot must run with the default `real` host mode.

## Operator flow

Set the instance and pilot once:

```bash
export SECRETARY_INSTANCE=/home/dev/secretary-instance
export PILOT_REF=secretary-000
```

Run preflight. It is expected to be blocked before old-owner pause evidence is
recorded:

```bash
python3 -m secretary dispatcher preflight \
  --instance "$SECRETARY_INSTANCE" \
  --pilot-ref "$PILOT_REF"
```

Freeze the old dispatcher using the current production procedure. This must be
`freeze`, not `drain`: `freeze` stops claims, advance, Validate, retry and the
watchdog. Run it from the legacy checkout or the provisioned pipeline worktree:

```bash
PYTHONPATH=/home/dev/triggered-agents BOARD_ROLE=steward \
  python3 -m triggered_agents pipeline pause freeze \
    --actor "$USER" \
    --reason "secretary dispatcher pilot cutover"
```

Verify the live pause state before recording it in secretary:

```bash
PYTHONPATH=/home/dev/triggered-agents \
  python3 -m triggered_agents pipeline pause-status
```

The status must show `paused: true`, `mode: freeze` and the live state path
under the production pipeline worktree. If it shows `drain`, resume and freeze
again. If it shows an automation-owned freeze that is auto-resume eligible,
resume and freeze with a human actor or disable the TTL for the cutover window.

The secretary command records operator evidence only after the live freeze check
passes. It does not claim the card:

```bash
cat > /tmp/old-dispatcher-paused.txt <<'EOF'
legacy dispatcher freeze verified with pause-status
EOF

python3 -m secretary dispatcher pause-old \
  --instance "$SECRETARY_INSTANCE" \
  --pilot-ref "$PILOT_REF" \
  --actor "$USER" \
  --evidence-file /tmp/old-dispatcher-paused.txt
```

Start the new pilot owner:

```bash
python3 -m secretary dispatcher start-new-pilot \
  --instance "$SECRETARY_INSTANCE" \
  --pilot-ref "$PILOT_REF" \
  --actor "$USER"
```

If this returns `status: blocked`, do not run `tick`. Fix the legacy pause state,
run `pause-old` again, then retry `start-new-pilot`. A blocked start does not
claim or move the pilot card.

Run ticks and observe after each step:

```bash
python3 -m secretary dispatcher tick \
  --instance "$SECRETARY_INSTANCE" \
  --pilot-ref "$PILOT_REF"

python3 -m secretary dispatcher observe \
  --instance "$SECRETARY_INSTANCE" \
  --pilot-ref "$PILOT_REF"
```

`observe` prints the current `attempt_id`. After the first successful tick,
verify the live board state before letting the worker continue:

```bash
python3 -m secretary task show --ref "$PILOT_REF"
```

The card must be `in_progress`, `claim.worker` must be the worker from the tick
output, and `routing.resolved_worker_head` plus
`routing.resolved_review_head` must match that attempt. If `tick` returns a
controlled divergence, do not start or resume a head manually; keep the legacy
dispatcher frozen and run rollback.

Expected pilot path:

1. Ready pilot card is claimed and moved to In progress.
2. Worker posts `secretary task report --kind done`.
3. Dispatcher moves the card to Validate and launches review.
4. Reviewer posts `secretary task verdict --kind green`.
5. Dispatcher moves the card to Done.

Record these facts before the first live tick and after the card reaches Done.
This is evidence for one pilot card only; it does not mark the full Phase 7 or
Phase 9 cutover complete.

- Active owner: legacy dispatcher freeze status, new dispatcher attempt id and
  pilot ref.
- Card and claim: board state, claim worker and latest dispatcher/comment marker.
- Workspace: worker and reviewer workspace paths plus branch names.
- PR, CI and review: PR URL, head SHA, CI conclusion and review verdict marker.
- Neighboring Ready cards: refs observed before and after the pilot, with no
  unexpected claims.
- Rollback state: whether rollback was unused, still available, or already
  executed, and the preserved card/PR state if it ran.

Commit cutover only after the pilot is green and the old owner is still paused:

```bash
python3 -m secretary dispatcher commit-cutover \
  --instance "$SECRETARY_INSTANCE" \
  --pilot-ref "$PILOT_REF" \
  --actor "$USER"
```

## Rollback

Rollback is an operator action. It does not reset the board card and does not
delete comments or claim metadata, so the old dispatcher can continue from the
same card state after the operator resumes it.

```bash
cat > /tmp/dispatcher-rollback.txt <<'EOF'
pilot failed; return ownership to legacy dispatcher
EOF

python3 -m secretary dispatcher rollback \
  --instance "$SECRETARY_INSTANCE" \
  --pilot-ref "$PILOT_REF" \
  --actor "$USER" \
  --reason-file /tmp/dispatcher-rollback.txt
```

After rollback, resume the old dispatcher through the current production
procedure and inspect the pilot card:

```bash
PYTHONPATH=/home/dev/triggered-agents BOARD_ROLE=steward \
  python3 -m triggered_agents pipeline resume
```

Then inspect the pilot card with:

```bash
python3 -m secretary task show --ref "$PILOT_REF"
```

If the rollback happened after the worker posted a PR and the card is in
Validate, verify legacy continuation before resuming the old dispatcher:

```bash
PR=$(python3 -m secretary task show --ref "$PILOT_REF" | sed -n 's/.*\(https:\/\/github.com[^ ]*\/pull\/[0-9][0-9]*\).*/\1/p' | tail -1)
BRANCH=$(gh pr view "$PR" --json headRefName --jq .headRefName)
test "$BRANCH" = "pipeline/$PILOT_REF"
git -C /home/dev/secretary fetch origin "$BRANCH"
git -C /home/dev/secretary rev-parse "origin/$BRANCH"
```

After `resume`, the legacy reviewer should create its own `review/<ref>`
worktree from that same PR head.

## Current owner

Until a post-merge live pilot is completed, production dispatcher ownership stays
with the legacy contour. The new runtime is the pilot owner only for the exact
`--pilot-ref` recorded in its state file.

Full cutover still requires:

- one low-risk live pilot after the branch is merged to `main`;
- before and after notes for active dispatcher owner, card claim, workspace,
  PR/CI/review state and absence of double claim;
- rollback if the live pilot is red.

## Production dispatcher

The production entrypoints are separate from the pilot commands and do not accept
`--pilot-ref`:

```bash
python3 -m secretary dispatcher production-observe \
  --instance "$SECRETARY_INSTANCE"

python3 -m secretary dispatcher production-tick \
  --instance "$SECRETARY_INSTANCE"

python3 -m secretary dispatcher production-run \
  --instance "$SECRETARY_INSTANCE" \
  --interval-seconds 60
```

Production mode is fail-closed. A tick mutates the board only when:

- `pilot-state.json` is in `phase: cutover_committed`;
- the live legacy pause probe confirms a hard `freeze`;
- `production-state.json` has no owner or is already fenced to the same owner;
- the singleton tick lock is available.

Production ticks first recover and advance existing `In progress` / `Validate`
cards, then scan the shared Ready queue in stable board order. The claim still
goes through `secretary task claim`, so the active code-task-per-project guard
remains the source of truth. If a project already has an active code task, the
scanner skips later Ready code cards for that project and can claim a different
project. A repeated tick or process restart reuses durable records and audit
request ids, so it does not create a second workspace, reviewer or merge.

State lives under `<data_dir>/dispatcher/`:

- `pilot-state.json`: pilot and committed cutover state.
- `production-state.json`: production owner fence and in-flight records.
- `production-tick.lock`: singleton mutation lock for one tick.
- `production-run.lock`: singleton long-running service lock.

`production-run` keeps the process alive, backs off on temporary backend or host
errors and continues after one card returns a controlled failure. Use
`production-tick` for smoke tests and manual diagnostics only.

## Production operator flow

Do not perform these live cutover commands as part of an implementation PR. Run
them only after the branch is merged and an operator has scheduled the cutover
window.

Set the instance:

```bash
export SECRETARY_INSTANCE=/home/dev/secretary-instance
```

Freeze the legacy dispatcher with the current production procedure and verify
that the live pause state is `freeze`, not `drain`:

```bash
PYTHONPATH=/home/dev/triggered-agents BOARD_ROLE=steward \
  python3 -m triggered_agents pipeline pause freeze \
    --actor "$USER" \
    --reason "secretary production dispatcher cutover"

PYTHONPATH=/home/dev/triggered-agents \
  python3 -m triggered_agents pipeline pause-status
```

Commit cutover state from the already completed pilot:

```bash
python3 -m secretary dispatcher commit-cutover \
  --instance "$SECRETARY_INSTANCE" \
  --pilot-ref "$PILOT_REF" \
  --actor "$USER"
```

Render the managed host plan. This is read-only and should show the production
dispatcher service and timer as managed resources, not silently adopt existing
unmanaged units:

```bash
python3 -m secretary reconcile plan --dry-run \
  --instance "$SECRETARY_INSTANCE"
```

Apply the managed service/timer through the operator-owned host deployment
procedure, then verify doctor before starting broad queue service:

```bash
python3 -m secretary doctor \
  --instance "$SECRETARY_INSTANCE" \
  --host
```

Smoke with one manual tick, then start the long-running service:

```bash
python3 -m secretary dispatcher production-tick \
  --instance "$SECRETARY_INSTANCE"

systemctl --user start secretary-dispatcher-production.service
```

Create or move a low-risk smoke card to Ready and watch it pass worker, review
and Done through `secretary task show`. Keep the legacy dispatcher frozen until
the smoke card reaches Done and `doctor --host` reports a single production
owner.

## Production rollback

Rollback before decommission keeps the legacy dispatcher frozen, stops the
managed production service and moves ownership back manually:

```bash
systemctl --user stop secretary-dispatcher-production.service
systemctl --user stop secretary-dispatcher-production.timer
python3 -m secretary dispatcher production-observe \
  --instance "$SECRETARY_INSTANCE"
```

Inspect active cards with `secretary task list` and resume the legacy dispatcher
only after the operator has confirmed no production tick is still running. Do
not force-reset task state; preserve claims, comments, PRs and review evidence
for manual continuation or retry.
