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



## Re-review packet

previous_reviewed_sha: 0b237071a57b583499e2865c9f04ee342f67c70f
current_sha: d67d2704eedb5f96ff6ebf0101faf9299da68a2a
Changed paths / delta from the prior review:
REVIEW.md secretary/dispatcher.py secretary/dispatcher_tui.py secretary/session.py secretary/task_restore.py tests/test_dispatcher.py tests/test_dispatcher_contracts.py tests/test_dispatcher_launch_intent.py tests/test_dispatcher_observer.py tests/test_dispatcher_tui.py tests/test_restore.py tests/test_session.py tests/test_triggered_dispatch.py triggered_agents/agents/pipeline/heads.py triggered_agents/runtime/dispatch.py triggered_agents/runtime/tui_delivery.py
REVIEW.md | 63 +++----- secretary/dispatcher.py | 19 ++- secretary/dispatcher_tui.py | 247 +++++------------------------- secretary/session.py | 7 +- secretary/task_restore.py | 23 +++ tests/test_dispatcher.py | 69 ++++++++- tests/test_dispatcher_contracts.py | 69 ++++++++- tests/test_dispatcher_launch_intent.py | 4 +- tests/test_dispatcher_observer.py | 16 +- tests/test_dispatcher_tui.py | 14 +- tests/test_restore.py | 40 +++++ tests/test_session.py | 27 ++++ tests/test_triggered_dispatch.py | 105 +++++++++++-- triggered_agents/agents/pipeline/heads.py | 60 ++++++-- triggered_agents/runtime/dispatch.py | 95 ++++++++---- triggered_agents/runtime/tui_delivery.py | 234 ++++++++++++++++++++++++++++ 16 files changed, 757 insertions(+), 335 deletions(-)
Previous blockers (close or explicitly retain these stable IDs):
# Review secretary-1173 Verdict: RED BLOCKER-legacy-alias-cross-family Reachable scenario: an installation registry can validly contain both a current `codex` TUI profile and a Claude profile named `codex-terra`; the registry validator does not reserve profile ids by adapter. A persisted worker or reviewer override of `codex-terra` then reaches `resolve_head_id`. Because that id is present, the resolver returns it before checking its adapter. `InstanceCatalog.worker_head` consequently returns `codex-terra`, whose adapter is Claude, and the launcher renders Claude rather than either resolving to a Codex TUI profile or failing closed. I reproduced this with a registry accepted by `validate_registry`; the result was `{"accepted_registry": true, "resolved_head": "codex-terra", "adapter": "claude"}`. This violates the former-Codex-id acceptance criterion: those ids must resolve to an equivalent available TUI profile, and an invalid non-Codex id must fail closed rather than silently move model family. The material assumption is supported by the public registry contract: profile ids and adapters are independently valid fields, so an installation can republish or accidentally reuse this id without failing validation. The cross-family resolution is introduced by this branch's legacy alias resolver; the pre-existing validator did permit the input but did not promise this substitution. Repair appears local to the alias resolver and its tests, with corresponding use through `session.resolve_profile_id`; it must make a direct legacy-id profile subject to the same Codex-family check as an alias candidate. This does not require a new profile family or a policy change. BLOCKER-restore-rewrites-retired-exec Reachable scenario: restore a checkpoint card whose metadata has `codex_launch_mode=exec`. `_restore_fields` correctly turns it into an empty mode for `TaskWriter.create`, but `import_normalized_board` then calls `writer.restore_card(metadata=_restore_board_metadata(card), ...)`.
Review this delta, the closure of prior blockers and collateral impact; do not restart
from the original base unless a concrete suspicion requires the historical diff.

## Mechanical gate attestation

- validated_sha: d67d2704eedb5f96ff6ebf0101faf9299da68a2a
- base_sha: 5762fdbe15a189b30c6e014bbed396c5b011c854
- gate_mode: github
- required terminal checks:
  - test: SUCCESS (https://github.com/vladmesh/secretary/actions/runs/31341728500/job/93316518263)
- completed_at: 2026-08-09T23:24:25+00:00
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
Write the body to /tmp/secretary-verdict-secretary-1173-10.md with your file-writing tool,
then run the command below verbatim. Do not assemble the body inside the shell command
(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses
rm-style commands, and quotes or backticks in the body break the call. Leave the file in
place afterwards; the dispatcher does not read it.
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1173 --role reviewer --kind green --request-id dispatcher-attempt-20260809T215934Z-a9a75bad7c73-review-green-secretary-1173-10 --body-file /tmp/secretary-verdict-secretary-1173-10.md
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1173 --role reviewer --kind red --request-id dispatcher-attempt-20260809T215934Z-a9a75bad7c73-review-red-secretary-1173-10 --body-file /tmp/secretary-verdict-secretary-1173-10.md
