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

## Current owner

Until a post-merge live pilot is completed, production dispatcher ownership stays
with the legacy contour. The new runtime is the pilot owner only for the exact
`--pilot-ref` recorded in its state file.

## Live pilot checklist

Record these facts in the pilot task report or operator log. The checklist is
evidence for one pilot card, not a declaration that the full Phase 7 or Phase 9
cutover is complete.

Before starting `tick`:

- active owner: legacy dispatcher is in verified `freeze` and the new dispatcher
  state names the exact `--pilot-ref`;
- card and claim: pilot card ref, state, claim worker and latest dispatcher
  comment;
- workspace: expected worker workspace path and branch;
- neighboring Ready cards: refs that must remain Ready and unclaimed;
- rollback state: whether prior pilot state is absent, already rolled back or
  intentionally reused for the same ref.

After the pilot reaches a terminal state:

- active owner: new dispatcher handled only the pilot ref while legacy stayed in
  `freeze`;
- card and claim: final state, claim worker and dispatcher comments show the
  expected In progress, Validate, review and terminal transitions;
- workspace: worker workspace still matches the claimed ref and was not removed
  by the legacy watchdog;
- PR/CI/review: PR URL, commit, CI result and independent review verdict;
- neighboring Ready cards: unchanged refs remain Ready and unclaimed;
- rollback state: rollback was not needed for green, or rollback preserved card,
  claim, comments, PR and review state for red.

Full cutover still requires:

- one low-risk live pilot after the branch is merged to `main`;
- before and after notes for active dispatcher owner, card claim, workspace,
  PR/CI/review state and absence of double claim;
- rollback if the live pilot is red.
