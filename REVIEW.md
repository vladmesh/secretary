# Review secretary-1173

# Goal

Make every Codex worker, reviewer and service-head launch through an interactive TUI only. No routing input, missing profile field or legacy card may reach `codex exec`; existing head overrides that name former Codex profile IDs must still resolve to an equivalent TUI profile.

# Context

The precondition from `issue:de0d51816838530cbb5b` is resolved on main: worker and reviewer bring-up now defer and retry a pane held in a dialog, with bounded failure, instead of requiring the former exec bypass. The live portable registry still leaves `codex`, `codex-high` and `codex-reviewer` without `codex_mode = "tui"`; `secretary/dispatcher_launcher.py:161-175,242-254,292-297` defaults a missing mode to `exec` and retains the exec renderer. `secretary/task_commands.py:108`, `secretary/tasks.py:1012`, `secretary/dispatcher.py`, `secretary/routing_journal.py` and restore parsing retain a per-card `codex_launch_mode` whose old default can select exec.

The sprint contract requires an absence of `codex_mode` to mean TUI, rejects `--codex-mode exec`, removes the production exec-renderer/launch branch, and preserves old Codex profile IDs rather than orphaning already-recorded `head_override` or `review_head_override`. Treat a persisted old exec field as legacy routing data that must resolve safely to TUI, not as authority to launch exec. Reuse the existing registry normalization/alias and TUI prompt-delivery paths; do not add a second launch transport, timeout workaround, compatibility process, or a new profile family.

# Acceptance criteria

- Every Codex profile in the portable registry and its generated installation registry is explicit TUI, including role defaults and fallback targets. Missing `codex_mode` resolves to TUI only.
- No production launcher can render or invoke `codex exec`: delete the exec renderer and mode branch, and make the launch result use the existing post-start TUI prompt delivery for Codex roles.
- `task create --codex-mode exec` is rejected before any board write. Persisted or restored legacy `codex_launch_mode=exec` cannot re-enable exec and the routing journal records the effective TUI mode. Preserve compatible non-exec legacy routing data only where needed to read existing records.
- Former IDs `codex`, `codex-sol`, `codex-terra`, `codex-luna`, `codex-5-4`, `codex-mini`, `codex-spark`, `codex-high`, `codex-extra`, `codex-reviewer`, `codex-curator`, `codex-steward` and `codex-retro` resolve to an equivalent available TUI profile for persisted worker/reviewer/service overrides. An unavailable or invalid non-Codex id still fails closed; do not silently substitute a different model family.
- Focused tests prove the default, explicit old exec and restored legacy routes all launch TUI; no command contains `codex exec`; create rejects exec without a write; the declared old IDs do not orphan overrides; and the dialog-deferred worker/reviewer TUI bring-up and current role-family routing remain covered. Update affected checkpoint/restore/journal fixtures rather than preserving an exec assertion.
- Report the committed SHA, clean worktree, clean diff and focused checks. A fresh exact-SHA mechanical gate remains required before review.

# Out of scope

Changing Claude launch behavior, worker/reviewer family-selection policy, watchdog timing, red-review continuation, pane identity, Orca itself, the recovery-canon path, live installation deployment, or rewriting any existing branch history.

## Operator-directed review retry (2026-08-10)

This round exists solely to take the already completed candidate `34bf7e7a6c5eafa61ae9d30068bfdd2b9b579a89` through an independent Claude review after the Codex reviewer-pane delivery failed twice. Do not modify files, create commits, or run any tests in this worker round. Confirm the retained workspace is clean and still at that candidate SHA, then immediately submit the ordinary `report:done` command from this TASK.md. State the SHA and that this was an operator-directed no-change/no-test handoff; do not claim a mechanical gate passed. The dispatcher must obtain its ordinary exact-SHA gate receipt before it starts the reviewer. This instruction overrides the development check-cost instructions for this no-change handoff only.


## Re-review packet

previous_reviewed_sha: 81122c645db2188ae384a6720aaa4035ddd856e2
current_sha: d451d90b0ef0ae001cc0dd1b525ed918131f3ddd
Changed paths / delta from the prior review:
REVIEW.md docs/ARCHITECTURE.md secretary/dispatcher.py secretary/dispatcher_launcher.py secretary/session.py tests/test_dispatcher_contracts.py tests/test_dispatcher_observer.py tests/test_triggered_dispatch.py triggered_agents/agents/pipeline/heads.py triggered_agents/runtime/codex_preflight.py triggered_agents/runtime/dispatch.py
REVIEW.md | 15 +- docs/ARCHITECTURE.md | 19 ++- secretary/dispatcher.py | 18 +- secretary/dispatcher_launcher.py | 213 +++--------------------- secretary/session.py | 6 +- tests/test_dispatcher_contracts.py | 47 ++++++ tests/test_dispatcher_observer.py | 21 ++- tests/test_triggered_dispatch.py | 137 ++++++++++++++- triggered_agents/agents/pipeline/heads.py | 80 ++------- triggered_agents/runtime/codex_preflight.py | 247 ++++++++++++++++++++++++++++ triggered_agents/runtime/dispatch.py | 125 ++++++++++++-- 11 files changed, 631 insertions(+), 297 deletions(-)
Previous blockers (close or explicitly retain these stable IDs):
# Review verdict: secretary-1173 (round 22) — RED Reviewed SHA: 81122c645db2188ae384a6720aaa4035ddd856e2 (HEAD of pipeline/secretary-1173, base 5762fdbe15a189b30c6e014bbed396c5b011c854). Worktree is clean apart from `REVIEW.md`, which carries this round's own review packet (dispatcher-owned, not a candidate change). I ran no tests. The attested `test` check on this exact SHA was not rerun and I record no `rerun_reason`; everything below is static inspection of the diff plus read-only inspection of this host's live registry and Codex home. ## What the branch does well Most of the contract is met, and met cleanly: - All four Codex profiles in the portable registry (`codex`, `codex-high`, `codex-reviewer`, `codex-observer`) now state `codex_mode = "tui"`; `validate_registry` defaults a missing mode to TUI and refuses `exec` outright. - The exec renderer is genuinely gone: `_render_codex_exec_command` in `secretary/dispatcher_launcher.py` and `_render_codex` in `triggered_agents/agents/pipeline/heads.py` are deleted, `ADAPTERS["codex"]` is the TUI renderer, and `render_codex_launch` has no mode branch or mode argument left. `render_command` now returns a `HeadCommand` carrying `prompt_after_start`, and both consumers (`secretary/dispatcher.py`, `triggered_agents/runtime/dispatch.py`) handle it. - `task create --codex-mode exec` is refused in `_validate_codex_mode_for_create`, which `run_task_create` calls before `TaskWriter`/`KanboardClient` is even constructed, so no board call can precede it. `TaskWriter.create` refuses the same value independently. - The legacy-routing story is coherent at every layer: `restore._restore_fields` maps a checkpointed `exec` to no mode, `task_restore._without_retired_launch_mode` clears it at the `saveTaskMetadata` write boundary (closing the round-16 blocker recorded in TASK.md), `_create_metadata_values` drops it on replay, and `routing_journal.head_run_from_profile` writes `CODEX_TUI_MODE` for every Codex adapter rather than copying t
Review this delta, the closure of prior blockers and collateral impact; do not restart
from the original base unless a concrete suspicion requires the historical diff.

## Mechanical gate attestation

- validated_sha: d451d90b0ef0ae001cc0dd1b525ed918131f3ddd
- base_sha: 5762fdbe15a189b30c6e014bbed396c5b011c854
- gate_mode: github
- required terminal checks:
  - test: SUCCESS (https://github.com/vladmesh/secretary/actions/runs/31345438273/job/93326381499)
- completed_at: 2026-08-10T00:50:54+00:00
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
Write the body to /tmp/secretary-verdict-secretary-1173-30.md with your file-writing tool,
then run the command below verbatim. Do not assemble the body inside the shell command
(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses
rm-style commands, and quotes or backticks in the body break the call. Leave the file in
place afterwards; the dispatcher does not read it.
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1173 --role reviewer --kind green --request-id dispatcher-attempt-20260810T000143Z-5541338d119d-review-green-secretary-1173-30 --body-file /tmp/secretary-verdict-secretary-1173-30.md
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1173 --role reviewer --kind red --request-id dispatcher-attempt-20260810T000143Z-5541338d119d-review-red-secretary-1173-30 --body-file /tmp/secretary-verdict-secretary-1173-30.md
