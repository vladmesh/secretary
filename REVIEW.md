# Review secretary-1175

## Goal

Remove the multiplicative task-audit scan from mass sprint status reads while preserving the public
status and sprint-status contracts.  This is the sole execution card for
`issue:0cf545aeb9b901f46885` and must land on `main` through the normal worker, gate, review and
dispatcher path.

## Context

The issue records the production symptom and baseline (`issue:0cf545aeb9b901f46885`).
`SprintReader._resume_freshness` in `secretary/sprints.py` currently calls
`TaskAudit(self.data_dir).events()` per sprint.  `secretary/status.py:_sprints` first calls
`SprintReader.list()` and then `reader.status()` for every sprint, making `secretary status --json`
linear in sprint count times the append-only audit size.  Direct `sprint status` must remain correct.

Choose the smallest in-call solution: reuse the existing audit reader and share one traversal's
relevant results between the sprint summaries in that operation.  Do not add a persistent index,
cache lifetime beyond the operation, dependency, compatibility shim, or a second product change.
Terminal (closed/stopped) sprints must not trigger an audit-based re-evaluation; open sprint
freshness must still reflect significant linked-card events.

## Acceptance criteria

- Any mass sprint-summary path, including `secretary status --json`, consumes the committed task
  audit at most once per operation, irrespective of the number of sprints.  `sprint list` does not
  introduce an audit read; a direct single-sprint status has no redundant repeat scan.
- Add focused regression coverage that instruments the audit traversal for one versus many sprints
  and proves the count does not grow.  Cover an open sprint with a significant later linked-card
  event so its freshness remains correct.
- Cover closed and stopped sprint summaries without audit-based freshness recomputation, while
  retaining the documented JSON object shape and meaning of `resume_freshness`, `status`, and
  `sprint status` fields.  Preserve existing supported legacy and invalid-resume behaviour.
- Keep existing focused tests green and run the full unit suite from the candidate worktree after
  verifying that `secretary.__file__` resolves there, not through the host `PYTHONPATH` checkout.
- In the worker report record reproducible before/after measurements on the same live-installation
  command, `secretary status --json`: exact command, input/data identity, repetitions or timing
  method, observed wall-clock values, and a concise comparison of output correctness.  Separate
  evidence actually collected from any waits or unavailable measurements.
- Report all changed files, tests and validation commands.  Do not alter dispatcher, Orca,
  reviewer or observer policy/lifecycle code, sprint budgets, routing policy, or public schema.

## Out of scope

- Fixing unrelated pipeline, reviewer, dispatcher, Orca or observer findings.
- A persistent audit index or cache, new dependency, public schema change, or second execution
  card/product change.
- Changing review policy, routing policy, sprint lifecycle or budgeting semantics.


## Mechanical gate attestation

- validated_sha: f1c437053775d5779fd5bd1278e24519fba169b0
- base_sha: 34da6d3d70c787130078e3f96c86ae4ac58a486f
- gate_mode: github
- required terminal checks:
  - test: SUCCESS (https://github.com/vladmesh/secretary/actions/runs/31376336107/job/93416306603)
- completed_at: 2026-08-10T09:52:51+00:00
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
Write the body to /tmp/secretary-verdict-secretary-1175-2.md with your file-writing tool,
then run the command below verbatim. Do not assemble the body inside the shell command
(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses
rm-style commands, and quotes or backticks in the body break the call. Leave the file in
place afterwards; the dispatcher does not read it.
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1175 --role reviewer --kind green --request-id dispatcher-attempt-20260810T090939Z-2d279b77f589-review-green-secretary-1175-2 --body-file /tmp/secretary-verdict-secretary-1175-2.md
PYTHONPATH="${TA_SECRETARY_REPO:-$HOME/secretary}${PYTHONPATH:+:$PYTHONPATH}" python3 -P -m secretary task verdict --ref secretary-1175 --role reviewer --kind red --request-id dispatcher-attempt-20260810T090939Z-2d279b77f589-review-red-secretary-1175-2 --body-file /tmp/secretary-verdict-secretary-1175-2.md
