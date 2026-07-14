# Dispatcher pilot cutover

This runbook covers the reversible Phase 7 pilot. The old dispatcher remains the
production owner until an operator pauses it and starts the new dispatcher against
one exact pilot card.

The product runtime lives in `secretary.dispatcher` and the CLI entrypoint is
`secretary dispatcher`. It uses the public `secretary task` protocol for all board
writes. The live runtime does not import Python modules from `triggered-agents`.

## Safety contract

- `--pilot-ref` is required for every dispatcher command. There is no broad Ready
  scan mode in the pilot runtime.
- `preflight`, `pause-old`, `start-new-pilot`, `tick` and `commit-cutover` read
  the live legacy pause file and require `freeze` (`mode: hard`). Operator
  evidence alone is not enough, and `drain` is not enough because the old
  dispatcher can still advance cards, run Validate and fire the watchdog.
- A stale automation-owned `freeze` that the legacy dispatcher would auto-resume
  is not enough. Use a human actor for the cutover window or disable the legacy
  hard-pause auto-resume TTL for that maintenance window.
- Cutover state is stored under `<data_dir>/dispatcher/pilot-state.json`.
- The new tick is serialized by `<data_dir>/dispatcher/pilot-tick.lock`.
- Rollback stops new dispatcher terminals through the host adapter and leaves
  the card, claim, comments, PR and review state unchanged.
- Worker worktrees are landed on `pipeline/<ref>`, the same branch name the
  legacy Validate/reviewer path fetches. A rollback during Validate therefore
  resumes against the existing PR head without a manual branch rename or push.
- Claude worker/reviewer profiles are prepared before launch by setting
  `projects["<workspace>"].hasTrustDialogAccepted = true` in the Claude config.
  The write is per-workspace and fail-closed: an unreadable, corrupt, symlinked
  or non-atomically writable config blocks the launch instead of opening an
  interactive folder-trust prompt.
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
again. If it shows a stale automation-owned freeze that is auto-resume eligible,
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

Expected pilot path:

1. Ready pilot card is claimed and moved to In progress.
2. Worker posts `secretary task report --kind done`.
3. Dispatcher moves the card to Validate and launches review.
4. Reviewer posts `secretary task verdict --kind green`.
5. Dispatcher moves the card to Done.

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
