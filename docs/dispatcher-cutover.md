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
- `tick` refuses to mutate the board unless `pause-old` and `start-new-pilot`
  have recorded matching state for the same pilot ref.
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

Pause the old dispatcher using the current production procedure. The secretary
command records operator evidence, it does not claim the card:

```bash
cat > /tmp/old-dispatcher-paused.txt <<'EOF'
legacy dispatcher hard-paused; active timer disabled or verified idle
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
same card state.

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
procedure and inspect the pilot card with:

```bash
python3 -m secretary task show --ref "$PILOT_REF"
```

## Current owner

Until a post-merge live pilot is completed, production dispatcher ownership stays
with the legacy contour. The new runtime is the pilot owner only for the exact
`--pilot-ref` recorded in its state file.

Full cutover still requires:

- one low-risk live pilot after the branch is merged to `main`;
- before and after notes for active dispatcher owner, card claim, workspace,
  PR/CI/review state and absence of double claim;
- rollback if the live pilot is red.
