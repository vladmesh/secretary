---
name: run-sprint
description: "Autonomously execute the single active product sprint until its goal and Definition of Done are reached: pick the next most impactful step, do research yourself, create one fresh card at a time, watch the pipeline and CI, read worker and reviewer conclusions, add only permitted hotfixes, run the final live validation and close the sprint document. Use only on an explicit `$run-sprint` call or a direct request to execute the active sprint end to end."
---

# Run Sprint

Take the active sprint to its goal. Do not end the work on a created card, an open pull request, a
pending CI run or an interim report.

## Fixed rules

- Exactly one substantive sprint card executes at a time.
- A new card is created only after the previous one has been fully analysed.
- Existing Ideas are not promoted to Ready. Create a fresh card from current understanding.
- Goal, Definition of Done, out of scope and stop conditions are not changed autonomously.
- Do research yourself. Do not create a research card and do not launch a reviewer for it, except in the
  rare case where a separate durable artifact is needed in a specific repository.
- A red review and ordinary rework are not a failure of the card.
- Do not reinstall, wipe or restore the live system.
- Do not take an action that could cut off the current session, the session manager, the shell or the
  control channel.
- Do not force-push and do not rewrite published history.

## 1. Recover state

1. Call `memory_search` with `caller=secretary` for the goal and the current step.
2. Read the sprint status pointer and the sprint document it names in the instance repository's knowledge
   directory.
3. If there is no active sprint, stop and suggest `$start-sprint`.
4. Read the live board, task audit, dispatcher status, the affected checkouts and pull requests.
5. If the status pointer is behind the board, recover state from the durable documents and the live system,
   and record the reconciliation in the execution log.
6. If the sprint already has an active card, keep watching it. Do not create a second one.

After every significant transition, update the sprint document and the status pointer through
`secretary knowledge write`: the chosen step, a created card, Blocked, Done, a hotfix, a decision gate,
validation, closure. The records must let a new session continue without a transcript.

## 2. Check whether the goal is reached

Before each new card, check the Definition of Done against the current default branches and the live
system. Do not create work if the goal is already reached. If the evidence is insufficient, the next step
can be a check you run yourself.

Scope hints are not a checklist. The path to the goal can be changed, dropped and rebuilt.

## 3. Choose the next step

Use the first rule that applies:

1. A check or investigation that could disprove the plan or remove architectural uncertainty.
2. A blocker of several likely subsequent changes.
3. Shared groundwork that lowers the cost of the remaining work.
4. A mandatory hotfix.
5. The largest direct contribution to unclosed Definition of Done items.
6. An acceptable local quick fix.
7. Any other minimal vertical increment.

Do not use a fabricated numeric score. Record in the execution log:

- why this step was chosen;
- which alternatives were considered;
- why they were deferred;
- the information or contribution to the goal you expect.

If the step is research, do it yourself, record the conclusions and return to choosing. Do not materialise
it as a card.

## 4. Create one card

The spec must be fresh, self-sufficient and limited to one repository. Use the task protocol and the
`spec-card` structure: Goal, Context pointers, checkable Acceptance criteria, Out of scope. The card is
always new and goes straight to Ready.

Pick the worker by the difficulty and nature of the task. Where possible the reviewer comes from another
family:

- Anthropic worker → OpenAI reviewer;
- OpenAI worker → Anthropic reviewer.

If the other family is temporarily unavailable, wait first or pick another of its profiles. If that blocks
the sprint for long, an independent reviewer from the same family on a different profile is allowed; record
the exception. Do not use `review-head none` automatically, except for a fully mechanical trivial change.

Record the ref, repository, heads and the link to the goal in the sprint document. Set the status pointer to
running with the current card.

## 5. Watch the card to a terminal outcome

Use recurring waiting and monitoring. Check board state, task audit, dispatcher health, the pull request and
CI. Do not end the turn while the card is in Ready, In progress or Validate, while checks are queued or
running, or before the pull request reaches a terminal result.

Do not interfere with ordinary red and rework cycles. Intervention is acceptable when the work has
observably gone against the sprint contract:

1. Name the specific Definition of Done item being ignored or the out-of-scope boundary being crossed.
2. Leave a comment.
3. Move the card to Ideas, keeping the branch and workspace.
4. Confirm that worker and reviewer are stopped and the dispatcher record is dropped.
5. Update the spec while the card is inactive.
6. Return the corrected card to Ready as a new attempt, or create a new card if the cut was wrong.
7. Record the preempt in the execution log.

Do not preempt an unusual but compatible implementation.

## 6. Analyse the result

After Done, read more than the headline:

- worker reports and comments;
- every review verdict and remark, including non-blocking ones;
- the pull request, the final diff, CI and the merge result;
- new constraints, false premises and deferred findings;
- the live state after a self-deploy, where applicable.

Record a short conclusion in the execution log. Then check the Definition of Done again and choose the next
step. A card does not have to close an item named in advance: what matters is the actual contribution to
the goal.

## 7. Handle Blocked

Identify the class of cause:

- an implementation defect: return to a supported rework or retry;
- a bad spec or a wrong cut: preempt, rewrite, or create a fresh card;
- a pipeline or runtime bug: apply the hotfix policy;
- access: record exactly what is missing and stop;
- a product decision gate: record the options and stop;
- a proven impossibility of the goal: record the evidence and stop.

Green health does not by itself return a Blocked card to work. Make the transition explicitly, after the
analysis.

## 8. Keep hotfixes narrow

A mandatory hotfix belongs in the sprint if the problem:

- blocks the next move;
- makes the result or the validation untrustworthy;
- threatens loss of work or data;
- breaks the pipeline so that autonomous continuation is impossible;
- prevents checking the Definition of Done.

A quick fix is also acceptable only when the problem is confirmed and local, does not change a product
contract, needs no architectural decision, is checked by an existing test or one small new one, and is
highly unlikely to open a chain of further problems.

If the defect belongs to the current card and the same code, fix it in that card's rework. A separate
pipeline bug becomes its own hotfix card and is executed first. Record other findings as deferred or as
Ideas without widening the sprint.

## 9. Repair an unavailable dispatcher

If the dispatcher can still execute cards, repair goes through an ordinary hotfix card. If the dispatcher
cannot execute its own card:

1. Confirm the failure and check whether a supported reconcile, resume or restart is enough.
2. Check active worktrees, branches and affected files for conflicts.
3. Create a separate branch from the current remote default branch. Do not edit it directly.
4. Perform the minimal repair yourself.
5. Where possible, call in a reviewer from another family, through the pipeline or directly in a separate
   session. If no independent reviewer is available, do an explicit self-review plus every mechanical gate,
   and record the exception.
6. Address the remarks, wait for CI and merge through an ordinary pull request, without force or rewriting.
7. Restore the dispatcher, verify it with a canary card and return to the original step.

The whole repair is recorded as a separate hotfix in the sprint document.

## 10. Preserve yourself

Pulling or fast-forwarding code, an ordinary upgrade, and restarting a leaf service are allowed, as long as
the service does not hold the current head or the control channel.

Before a stop or restart, determine the process and session dependencies of the current session. If the
action could cut off your session, the session manager or the shell, or if it requires a wipe, reinstall or
destructive recovery:

1. Do not perform it.
2. Record an external runbook and the exact post-check.
3. Set the status pointer to waiting for an external action.
4. Stop. A new session continues after the external action.

An isolated disposable target is acceptable as long as it does not change the live installation, its
instance remote, systemd or data.

## 11. Run the final validation

Validation is run by you, not by a separate card. Choose the minimal sufficient real check:

- pipeline: create a canary card and take it through the new path;
- memory: run the ordinary write, read, search and supersede lifecycle on a test fact;
- projects: create a temporary project and go through add, provision, gate and smoke;
- host and runtime: check status, doctor, ownership and real behaviour;
- install and recovery: use only an isolated disposable target, or stop for an external check of the live
  installation.

If validation is red, add a hotfix or the next product step and continue the loop. Do not weaken the
Definition of Done.

## 12. Close the sprint

After a green validation:

1. Re-read the Ideas the sprint touched.
2. Archive the ones it absorbed and the ones that went stale, with an explanation.
3. Refresh the Ideas that are still useful, but do not promote them to Ready.
4. Leave the rest of the backlog alone.
5. Fill in decisions, deferred items and results: the goal reached, validation evidence, cards, hotfixes,
   important conclusions.
6. Set the sprint frontmatter status to closed.
7. Update the status pointer: no active sprint, state closed, with a link to the last sprint.
8. Check the knowledge write, the task audit, that the checkouts are clean, and that the sprint has no
   active cards.

Do not start the next sprint automatically.

## Permitted stops

You may stop only if:

- the goal is provably unreachable;
- a high-level decision from the stop conditions is required;
- access is missing;
- an external self-terminating action is required.

In every case, save durable state first: evidence, the exact question or runbook, the current refs and a safe
next step. Do not present an intermediate stop as a finished sprint.
