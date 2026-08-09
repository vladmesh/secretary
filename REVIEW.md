# Review secretary-1170

# Goal

Make `secretary upgrade` publish the tracked `heads/heads.yaml` and `heads/source.yaml` as one
durable recovery-canon update. A successful upgrade must leave the instance worktree clean, place a
checkpoint containing the actual source revision on the configured remote, and let a clean recovery
load the same registry before another upgrade runs.

# Context

`secretary/upgrade.py:324-350` regenerates the installed snapshot and source pin, but neither it
nor `CheckpointWriter` commits those tracked files. `secretary/checkpoint.py:58-70,288-317` already
provides scoped instance-repository commits, while `secretary/state_repo.py:29-64,133-174` supplies
the common writer lock, scoped path ownership, safe Git execution and commit identity. The live
registry reader in `secretary/head_registry.py:277-306` intentionally reads only the installed
snapshot, so a recovered stale pair silently routes the wrong heads until an upgrade.

The selected route is to reuse this existing recovery-writer model. The snapshot and pin remain
tracked canon, are published under `state_repo_lock` through a narrow owned pathspec, and the
upgrade's success condition includes its checkpoint publication. Do not replace them with ignored
derivatives: that would require a separate recovery/bootstrap design before the first upgrade and
would not satisfy this sprint's clean-target invariant. Do not add a second writer lock, a new
dependency, or a broad `git add`.

# Acceptance criteria

- When `step_head_registry` changes either generated file, upgrade publishes the pair through the
  existing common instance-repository writer lock with a pathspec limited to the two `heads/` files;
  no state, secret, memory, knowledge or unrelated worktree change is staged.
- A successful non-dry-run upgrade leaves the instance checkout clean and its remote checkpoint
  contains the matching `heads.yaml` and `source.yaml`; `source.yaml` records the actual canonical
  source path and product revision used for that snapshot.
- If the required commit or remote publication cannot complete, the upgrade does not report success
  and retains a durable, actionable failure. It must never claim a published recovery pair while the
  remote still has a different one.
- A fresh clean target restored from the published checkpoint loads the same validated head registry
  before its first subsequent `secretary upgrade`; a stale or incomplete pair fails closed with a
  bounded diagnostic instead of silently routing from an unrelated product checkout.
- Focused tests cover paired changed/unchanged upgrades, scoped staging in a dirty instance tree,
  commit or push failure, source-revision accuracy, and clean-target recovery. Preserve existing
  checkpoint serialization and no-foreign-divergence behaviour. Report focused checks, clean diff,
  committed SHA and clean worktree; a fresh exact-SHA mechanical gate remains required before
  review.

# Out of scope

Untracking or regenerating the pair as a new derivative bootstrap model; redesigning global
checkpoint cadence; changing product head routing, Codex TUI policy, terminal lifecycle, secret
store, or the live Orca installation.


## Mechanical gate attestation

- validated_sha: ae3050c4c2ada8731e46f88d9347927bec547327
- base_sha: 9fa8dc95c98a3b7b45577820108a26924efd1bf3
- gate_mode: github
- required terminal checks:
  - test: SUCCESS (https://github.com/vladmesh/secretary/actions/runs/31335680007/job/93301015034)
- completed_at: 2026-08-09T21:02:05+00:00
- command_or_check_set_digest: e05e08ff6e9e51da3be176a7b5215dfddd2f768f01036631e8a3c9ab7be723ca

Independently inspect the diff, acceptance criteria and invariants. The attested broad
check above already passed on this exact SHA: do not rerun that broad command or suite on
the same SHA unless you record a concrete `rerun_reason`. A focused reproduction is allowed
for a new blocker, an uncovered external behaviour, or a security/data-loss high-risk need.
Mandatory CI and the exact-SHA pre-merge gate remain machinery-owned and are not waived.

A red verdict must list every blocker you have found in this round. Prefix each with a
stable `BLOCKER-<short-slug>` id so a re-review can close it without rediscovering it.
Do not hold blockers back for a later round and do not widen the scope on the next one.

For every RED blocker, state the concrete reachable scenario, the violated acceptance
criterion or operational invariant, material assumptions, whether this branch introduced
the defect or it was pre-existing, and whether the repair appears local or would change
architecture, a compatibility promise, a product contract, or a trust boundary. Report
evidence; do not silently widen the supported boundary or decide sprint scope.

When a change depends on how an external backend behaves, a passing fixture is not
evidence: it can encode the same wrong assumption as the code under review. Say which
real behaviour you verified and how. If no end-to-end check against the real backend
was possible, write plainly that it was not done and which assumption stays unverified.

Post exactly one review verdict through the secretary task protocol:
Write the body to /tmp/secretary-verdict-secretary-1170-11.md with your file-writing tool,
then run the command below verbatim. Do not assemble the body inside the shell command
(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses
rm-style commands, and quotes or backticks in the body break the call. Leave the file in
place afterwards; the dispatcher does not read it.
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1170 --role reviewer --kind green --request-id dispatcher-attempt-20260809T204829Z-397ef30b5877-review-green-secretary-1170-11 --body-file /tmp/secretary-verdict-secretary-1170-11.md
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1170 --role reviewer --kind red --request-id dispatcher-attempt-20260809T204829Z-397ef30b5877-review-red-secretary-1170-11 --body-file /tmp/secretary-verdict-secretary-1170-11.md
