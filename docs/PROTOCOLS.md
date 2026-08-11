# Protocols

`--instance` accepts either an instance directory or a direct path to `instance.yaml`. The instance
holds installation configuration and the portable checkpoint in `state/`; the data directory holds
local mutable and derived runtime state.

## Checks and host ownership

```bash
python3 -P -m secretary doctor --instance INSTANCE
python3 -P -m secretary doctor --offline --instance INSTANCE
python3 -P -m secretary doctor --instance INSTANCE --host-fixture DIR
```

`doctor` is always read-only. A normal run checks config, data and live inventory; `--offline` keeps
only config and data; `--host-fixture` replaces live inventory with a deterministic fixture. A fixture
cannot be combined with `--offline`. Exit code `0` means the check completed with no findings, `1`
means findings (or warnings under `--strict`), `2` means invalid input or unreachable inventory.
Without `--strict`, warnings alone stay green.

Live parity is derived from the same desired state as `reconcile`: each project checkout is checked
against the normalised absolute path from its binding, including a path outside the projects root; the
projects root itself is only needed to find unmanaged checkouts. An unreachable or unnormalisable
expected checkout makes project inventory unavailable and yields code `2` rather than a missing-on-host
finding. Unit files, session-manager registrations and the required enabled/active state of
long-running services and timers are checked. A missing resource or an unhealthy required runtime state
is a finding and code `1`; a oneshot service may be inactive. Units listed in `foreign_units` are
excluded from managed parity and are not conflicts.

```bash
python3 -P -m secretary reconcile plan --instance INSTANCE [--host-fixture DIR]
python3 -P -m secretary reconcile adopt --instance INSTANCE --logical-id ID [--yes]
```

`reconcile plan` reads desired state and inventory, applies nothing and writes no manifest.
`--offline` is deliberately rejected. Code `0` means a plan without conflicts, `1` means conflicts, `2`
means invalid input or unreachable inventory.

`reconcile adopt` touches one existing desired session-manager registration. It checks the name and the
normalised repository path, shows a fingerprint, and stays a preview without `--yes`. A confirmed run
atomically adds a managed record without changing the session manager, systemd or worktrees. Unit
resources are not adopted through this path.

## Tasks

The public path to the board is `secretary task`. A card carries a `ref`, project, type, state,
dependency, claim, routing, workspace, retry and audit metadata:

```text
Issues → ready → in_progress → validate → assessment → done
                         └───────────────────────────→ blocked
```

The board columns are `Issues, Ready, In progress, Validate, Assessment, Blocked, Done`, in that
order.

`assessment` is a durable wait for a decision, not for a machine. A substantive reviewer verdict,
green or red, parks the card there: the reviewer is stopped, the worker of the round stays suspended
with its workspace, and nothing merges or reworks until somebody decides. Mechanical gate outcomes
never pass through it: CI, the stand run and the pre-merge re-check all resolve in Validate, so a
card that is parked has passed everything a machine decides. The steward may move a card from
Assessment to Blocked with a reason, its usual escalation when nobody comes back for a decision;
workers and reviewers move nothing, there as everywhere else. A card left in Assessment past the
steward's stale threshold is reported like any other stuck card.

## SHA-bound mechanical gate evidence

Every real mechanical gate result materializes a reusable receipt bound to one checkout:
`validated_sha`, `base_sha`, `gate_mode`, terminal `required_checks` (name, conclusion and URL),
`completed_at`, and a `command_or_check_set_digest`. For a local gate this is the configured command's
digest; for GitHub it is a stable required-check-set identity, not a workflow-definition or run digest.
Both object IDs are full 40-character SHA-1 or 64-character SHA-256 values; abbreviations are not
evidence. A local gate captures HEAD before and after its command and fails closed if the command moves
it. Only `local` and `github` may carry receipts; `none` and noop are valid only without one, and an
unknown mode is never accepted.
The dispatcher persists the receipt with the active card, renders it into the reviewer's task
document, replaces it with the fresh post-review receipt in the Assessment delivery, and writes the
fresh final receipt into the release audit after the mandatory exact-SHA pre-merge re-check. That
document is written outside the checkout, under the run artifacts and private to the installation,
because a workspace's identity is the tracked diff and untracked files a receipt hashes; the pane
receives only a bounded pointer to it. `TASK.md` is likewise a generated, git-ignored workspace
handoff packet. These are operational projections, not repository documentation or candidate
changes. A receipt is evidence, not permission to skip the pre-merge check or independent review.

## Candidate history

Before a gate publishes or validates anything, the dispatcher reads the candidate's own commit
messages over `base..HEAD` and rejects forbidden AI attribution: a `Co-Authored-By:` trailer
belonging to a coding agent. The check is deterministic and dispatcher-owned, it covers the common
agents whatever runtime wrote the commit, and it leaves ordinary human co-authorship alone. It runs
in every validation mode — `none` opts out of mechanical validation, not out of this boundary,
because a project without a gate merges its candidate just the same — so a violation is a red gate
with the commits named and a local repair spelled out: `git commit --amend` or `git rebase -i` in
the worker's own checkout, then report done again. Nothing is rewritten and nothing is force-pushed
for the worker, then or later. Every worker launch packet carries the same instruction, independent
of model family, and the reviewer reads the commit messages again as defence in depth.

A commit message is untrusted input written by the head under review. Two rules follow. Messages are
never framed against each other on a delimiter a message could contain: the object ids are listed
first from `%H`, which message text cannot influence, a listing that is not object ids is refused,
and each message is then read on its own. An addressed co-author is rejected only when its complete,
normalized name/address pair is in the narrow registered-agent list: neither a vendor domain nor an
ambiguous local part is evidence by itself, because either can belong to a human. A trailer with no
address at all is compared against the agents' exact full names. Anything the check
cannot read — a missing workspace, a base that resolves nowhere, an unreadable message — fails
closed: the gate cannot say what it would publish, so the card stops rather than the boundary being
skipped.

A gate that could not reach its backend gave no verdict, and the dispatcher does not read that
absence as a red one. A timeout, a TLS or DNS failure, a dropped connection or a 5xx served by the
backend itself leaves the card exactly where it is — no board move, no head stopped, no verdict or
decision spent — and the same question is asked again on the next tick. Every retry is one
`gate-transport-retry` action in the tick output, carrying the attempt number and the transport
error. The retries are bounded (`SECRETARY_GATE_TRANSPORT_MAX_ATTEMPTS`, five by default) and count
consecutive silence only: any answer, green, red or pending, starts the budget over. Once the budget
is spent the card moves to Blocked with a reason that names the transport and the last error, so an
operator reads it as a network failure and not as a judgement on the branch. This holds on every
path where the dispatcher asks the gate backend: the pre-review gate, the pre-merge re-check under a
green verdict, and the release re-check of a parked decision. An answer that did arrive keeps
deciding as before — a failed required check is still a red gate and still returns the card to its
worker.

"No answer came back" is decided where the question is asked, not afterwards from the wording of an
error. Every remote question the gate puts — the base fetch, the branch publish, the open-PR probe,
the PR create, the repository name, the check rollup, the failed-job log — goes through one call
helper, and only that helper raises the transport failure. A step that talks to nothing therefore
cannot produce one: a local validation command that hangs past its own ceiling is a determinate
answer about the branch and blocks the card immediately with that reason, however its message reads.
Each call carries the tool's own output into that decision, so a probe never converts silence into a
positive fact about the backend's state — an unanswered open-PR probe is not "there is no PR". Where
a failed remote command still has to be sorted into answered and unanswered, that judgement lives
inside the helper, and it recognises the answer rather than the failure: an HTTP status the tool
quotes (unless it is a 5xx, which is the backend failing to serve one), a GraphQL error or a
response body it parsed, or git's push report from the remote. Anything else a backend call prints —
a transport message, an empty stderr, a wording nobody has captured yet — is silence, and the card
waits. The default runs that way round on purpose: a wrong "no answer" costs a few retries and a
Blocked reason that quotes the tool, while a wrong "answer" costs an immediate Blocked on a moment
of bad network, which is the failure this contract exists to prevent.

A reviewer that cannot be started is a failure of the review stage, not a verdict on the candidate.
A split pane that will not open, an unavailable reviewer resource, or an unwritable launch intent:
the card holds its green gate receipt, its candidate SHA, its report round and request ids and its suspended worker
session, and the next tick launches the reviewer again against that same evidence. It does not move
through Ready, does not launch a worker, does not re-run the mechanical gate or any broad validation,
does not regenerate the candidate, and charges the sprint no budget event; each attempt is one
`review-infrastructure-retry` action naming the held candidate. The retries are bounded
(`SECRETARY_REVIEW_INFRA_RETRY_ATTEMPTS`, ten by default) and count consecutive failures only. Once
the ceiling is reached the card moves to Blocked with a reason that names the infrastructure, the
untouched receipt and the candidate SHA, so recovery is another reviewer launch rather than a new
worker round over the same code. An inventory that will not answer is
different: it cannot prove whether a reviewer is already live, so it preserves launch ambiguity
and retries the inventory without launching another head or consuming the headless-failure ceiling.

Workers use focused checks while developing and run no more than one local broad suite for a report
generation/unchanged SHA unless they state why it was rerun. That broad run goes through
`secretary check broad`, which streams the combined output, returns the check's own exit status and
writes a workspace-local receipt under the ignored `state/checks/` path: command and check-set digest,
cwd and imported project provenance, start/end/duration, exit code, parsed verdict and counts where the
runner prints them (scanned off the stream, so cleanup output after a summary cannot erase it), and a
bounded diagnostic tail. The receipt is evidence about content, not about time: it records the
checkout's HEAD object id and a digest of the tracked diff and untracked files, so
`secretary check show` answers whether it still describes the code in front of the role. While a usable
receipt exists, rerunning the broad suite only because the pane scrolled its output away is prohibited;
a changed SHA, an edited worktree or a concrete red result being fixed opens a justified new run, named
in the report. A receipt only ever claims an import it observed from the process that ran the check:
the `--module` shape runs the suite itself and records what that process imported, while an arbitrary
`--command` shell — which may change directory or import environment before any interpreter starts —
attests no import and is never reused in place of a run. Reuse asks more than that the import was
observed: it has to have resolved inside the candidate workspace, so a legitimate import environment
that resolves the project to another checkout is recorded truthfully and still refused. One predicate
answers that question for every route, and a check is keyed by its structured check set — shape,
module and exact argument vector — so two invocations that render alike cannot answer for each other. Anything less than an intact, finished receipt
with observed provenance for exactly this content — a truncated or edited artifact, a result no run
could have written, a killed or timed-out run, a checkout with no resolvable identity — is not a
summary and does not attest anything. The receipt never leaves the workspace and is never committed: only an
executed local/GitHub gate with a valid exact-SHA receipt is authoritative reusable evidence
downstream. A none/noop gate or missing
receipt attests no broad suite, so the role runs or requests validation appropriate to the decision.
Reviewers independently inspect changed code and invariants, but do not repeat an attested broad command
on the same SHA without a recorded `rerun_reason`; targeted reproduction remains appropriate for a new
blocker, uncovered external behaviour, or security/data-loss risk. Re-review
packets carry the previous reviewed SHA, previous blocker text/IDs, current SHA and changed-path delta,
so the next reviewer verifies the delta and closure rather than restarting at the original base.

Observers consume the worker report, reviewer verdict and gate receipt before code/CI exploration. A
valid executed exact-SHA receipt suppresses its routine broad rerun; none/noop or missing evidence does
not, and permits appropriate focused or broad validation. Contradictory evidence, RED/Blocked
classification, a real Definition-of-Done gap, or a security/data-loss concern also requires research.
The role that owns a further broad rerun records its reason in the report; a receipt never transfers
ownership of an unexplained rerun or suppresses a targeted reproduction of a new concrete risk.

A card parks only where a decision can come from: its sprint is open and declares a concrete
observer head. A card with no linked sprint, or one whose sprint declares `--observer none` or has
closed, keeps the immediate behaviour, where the verdict acts on its own tick.

The decision and its effect are two writes. The observer records the decision on the card, and the
dispatcher performs it: merge and `assessment -> done` for a release, a new worker round and
`assessment -> in_progress` for a rework, `assessment -> blocked` for a reslice.

```bash
python3 -P -m secretary task decide --role observer --ref PROJECT-N \
  --kind release --reason-file REASON.md --request-id REQUEST_ID
```

`--kind` is `release`, `rework` or `reslice`, and the reason is required. The observer is the only
role that decides; a PO that has to intervene moves the card itself, and on a sprint-reserved
project that move carries `--sprint-override` and a reason, which reads in the audit as the
override it is.

The move out of Assessment carries the decision it performs, and each decision has one destination:
`release` goes to Done, `rework` to In progress, `reslice` to Blocked. A `--decision` that names one
the card's audit does not hold since it entered the column is refused, whoever passes it, as is one
paired with the wrong destination; that is what makes the seam checkable from the audit alone.
Needing a decision at all binds the dispatcher, the role that performs them: a dispatcher move to
Done or In progress without `--decision` is refused, and so are `assessment -> ready`, `-> validate`
and `-> issues`, each of which leaves the column with nothing decided while Ready additionally
clears the claim. The PO is held to none of that, because its move is the escape hatch out of a seam
that is stuck. `assessment -> blocked` takes no decision from anyone: it is the escalation path the
steward and the dispatcher's own failures use, and a card that cannot be blocked is a card nothing
can rescue.

The observer takes no exit out of Assessment at all, not even one carrying a matching decision. A
matching move is checkable but it is not a release: the board would read Done with nothing merged.
The observer's authority over a parked card is `task decide`; the effect is the dispatcher's. The
PO's override and the steward's escalation to Blocked are the two moves out of the column that do
not go through the dispatcher.

An observer decides only about a card whose project an open sprint reserves, the same guard
`task move` carries. A decision on a card no open sprint holds is refused.

A release the dispatcher cannot carry out never leaves the card looking reworkable: it takes the
card to Blocked with the failure on it, whether the merge was rejected or the pre-merge re-check
went red while the card was parked. Deciding again on a release that failed part-way through is a
separate card; until it exists, Blocked is the one answer, because it cannot publish twice.

`secretary task move` is the writer for the transition itself. The board has one role and transition
model, this one; the `triggered_agents pipeline` surface is a consumer of the same board and carries
only the steward's `Assessment -> Blocked` escalation. `--target` is accepted as a second spelling
of `--to` on that command; both name the same destination state.

```bash
python3 -P -m secretary task list --project PROJECT
python3 -P -m secretary task show --ref PROJECT-N
python3 -P -m secretary task list --sprint sprint:ID
python3 -P -m secretary task create --role po --project PROJECT --type code \
  --title TITLE --state ready --head codex-extra --sprint sprint:ID
python3 -P -m secretary task archive --role po --ref PROJECT-N \
  --reason-file REASON.md --request-id REQUEST_ID
python3 -P -m secretary task edit --role po --ref PROJECT-N \
  --body-file SPEC.md --head codex-terra --review-head claude-opus
python3 -P -m secretary task create --role po --project PROJECT --type code --title HOTFIX \
  --sprint-override --sprint-override-reason-file REASON.md
```

`create` accepts `--description` or `--body-file`, plus dependency, workspace and routing fields.
A new execution task requires `--sprint`: the sprint must be open and the task project must be one
of its reservations. A closed sprint and an unreserved project are separate errors, both before the
first backend write. Tasks never accept product priority; `--priority` is rejected rather than
ignored. Execution tasks are created in Ready, never in Issues; the worker, reviewer and retro roles
create nothing but a proposal in Issues, which a PO later triages to Ready.

When `task create` omits `--ref`, Secretary allocates `PROJECT-N` from the project's board-wide
high-water mark across both open and closed cards. Allocation, staging and `createTask` are one
locally serialized operation, so concurrent local creators cannot reserve the same reference. The
pending audit first records the chosen reference and, once the backend returns it, the Kanboard task id;
a recovered pending create verifies and repairs only that durable recorded backend task id, including the
legacy blank-reference rules. An older id-less pending create has no automatic adoption or repair path:
it remains fail-closed for manual resolution rather than searching for a plausible live row or creating a
duplicate.

`--codex-mode` is valid only for a worker profile on a `codex` adapter, and `tui` is the only
value it takes. Every Codex head is one interactive session that the launcher brings up empty and
then sends the prompt into; the one-shot `codex exec` head is gone, so `--codex-mode exec` is
rejected before the board is touched, a head profile that names it fails registry validation, and a
card restored or read with it carries no launch mode at all.

`archive` closes an execution task in the backend and removes it from ordinary active listings without
deleting board history. It is PO-only, requires a non-empty reason, writes append-only audit and
supports idempotent retry through `--request-id`. Only a card with no live work can be archived:
in-progress and validate cards, and cards with an active claim, are rejected. A card closed from Done
stays a satisfied dependency; a card closed from any other column is not Done and does not unblock
anything. It cannot close a Product or Issue: use `secretary issue close` for the latter.

`edit` replaces a card's spec in place: `--title`, `--description`/`--body-file` (the full new text, not
a diff), `--head`, `--review-head`. PO, dispatcher and observer may edit, but an ordinary card is only
editable in Ready or Blocked: on an active card the worker is working against a snapshot of the
task document, so an edit goes through preempt and requeue rather than a silent swap. The `edited` audit
event records the old and new digests; the full text of past versions is recoverable from the Git history
of the board export in the checkpoint. Comments stay the dialogue of an attempt; the spec lives only in
the description.

## Products and issues

`secretary product` and `secretary issue` use typed records in the existing Pipeline backend. They do
not introduce a file or a second board as a competing source of truth, so normal board export,
checkpoint and restore carry their metadata and comments.

```bash
python3 -P -m secretary product create --role po --id secretary --project secretary --title Secretary
python3 -P -m secretary issue create --role po --product secretary --kind feature --priority P2 --title TITLE
python3 -P -m secretary issue list --product secretary
python3 -P -m secretary issue show --ref issue:123
python3 -P -m secretary issue update-priority --role po --ref issue:123 --priority P1 --reason REASON
python3 -P -m secretary issue close --role po --ref issue:123 --reason resolved
```

A Product id is stable and its non-empty project set must contain only ids registered under the
instance `projects/` directory. Product ids cannot be duplicated. Every new issue requires its
Product, one kind (`bug`, `feature`, `question`, `improvement`) and one priority (`P0` through `P3`).
Priority changes require a non-empty reason, add an `[issue:priority]` board comment and append a
durable audit event. Only the PO may close an issue, using exactly one of `resolved`, `invalid`,
`duplicate` or `wont_do`; closure archives the backend record but leaves its comments and audit
history available through `issue show --ref` and checkpoint recovery. `issue list --closed` includes
both open and closed issues; without it the list contains only open issues.

A Product and an Issue are not execution tasks and never enter the execution columns: `move` and `claim`
both reject one before any write, whatever column it currently sits in. Work on an issue is a separate
card the PO creates in Ready.

A record belongs to the board rather than to one project, so every Product and Issue row is created in a
single lane: the board's first active swimlane in the board's own order, position first and the swimlane
id as the tie-break. The order is a property of the board, so concurrent writers and retries choose the
same lane, and a board without named swimlanes keeps Kanboard's implicit default lane.

Every Product and Issue write is staged before it touches the backend, and a staged write that is neither
finished nor dropped blocks checkpoint and board export. A refusal that a retry cannot turn into a success
therefore has to end the transaction rather than leave it: a `createTask` the backend declines is reported
as `backend_rejected`, and once the board shows no row of that request the staged document is dropped with
it. `validation` and `closed` refusals before the first backend write end the same way.

A staged write that did reach the backend stays, and belongs to its own request id. Its supported repair is:

```bash
python3 -P -m secretary product transaction list
python3 -P -m secretary product transaction retry --request-id REQUEST_ID
python3 -P -m secretary product transaction discard --request-id REQUEST_ID
python3 -P -m secretary product transaction adopt --path FILE
```

`retry` finishes the staged operation exactly where it stopped and commits its one audit event; a request
already committed is answered with its record. `discard` drops a transaction only after reading the board:
a create whose row exists and a priority or close change whose board comment exists are refused as
`live_write` and have to be retried instead. `adopt` files a transaction document that lives outside the
journal back under its own request id, which is how a document carried out of the journal comes back into
`retry` or `discard`. The commands cover Product and Issue writes alike; the journal is one.

## Sprints

A sprint is a data entity on a separate `Secretary sprints` board, not a Pipeline card. One board task
is one sprint. The board is created lazily and idempotently, so a repeat call creates no duplicate. A
reference has the form `sprint:ID`, a separate namespace from the `PROJECT-N` card convention.

```bash
python3 -P -m secretary sprint create --role po --goal GOAL --dod-file DOD.md \
  --product PRODUCT_ID --issue issue:ID --project PROJECT_ID \
  --observer HEAD_PROFILE --repository REPO --request-id REQUEST_ID
python3 -P -m secretary sprint list --status open
python3 -P -m secretary sprint show --ref sprint:ID
python3 -P -m secretary sprint status --ref sprint:ID
python3 -P -m secretary sprint comment --role worker --ref sprint:ID --body-file NOTE.md
python3 -P -m secretary sprint current-task --role dispatcher --ref sprint:ID --task PROJECT-N
python3 -P -m secretary sprint budget --role dispatcher --ref sprint:ID --type red_ci
python3 -P -m secretary sprint resume --role observer --ref sprint:ID --body-file RESUME.json
python3 -P -m secretary sprint reopen --role po --ref sprint:ID --observer HEAD_PROFILE
python3 -P -m secretary sprint close --role po --ref sprint:ID
```

Stored fields are the goal, the Definition of Done text, repositories, the owning product, its issues,
the reserved projects, open/closed/stopped status, the declared observer, a
budget counter by event type, the current card and a structured resume entry. The six valid budget event
types are `red_review`, `blocked`, `red_ci`, `preempt`, `recreated_task` and `hotfix`. Production derives
them from durable card audit events: a red review, a move to Blocked, a red mechanical gate, a preempt of
an active card back to Ready, or a tagged recreation or hotfix creation. The card-event id becomes the
budget request id, so a repeated tick cannot charge it twice. Green cards and observer activity have no
matching event and do not move the counter.

A new sprint belongs to a Product, serves at least one of its open Issues and reserves at least one
registered project. `--product` names an existing Product, every `--issue` is an open Issue of that
Product, and every `--project` is an id from the instance project registry; `--repository` keeps its own
meaning as the write-guard scope. A repository root is canonicalized where it is declared, so the row
persists the absolute path and a root this host cannot resolve is refused with the value it could not
resolve. An Issue of another Product and a closed Issue are refused with their
own messages. By default one installation holds at most one open sprint, and the gated pilot that raises
that to two is [below](#the-open-sprint-limit), which is also where what admission checks and in what
order is stated. Every one of these checks is a read, so a
refused sprint leaves no board row, no metadata and no audit event. A repeated `--request-id` still
returns the first event instead of colliding with the sprint it already opened.

Because the rules are reads of live state, `create` and `reopen` hold one exclusive lock on the data
directory (`sprints/admission.lock`) across the check and the write it admits. Two writers on the same
installation are serialized by it, so the second sees the sprint the first opened rather than a state
from before it. The lock is an admission gate only: it holds no sprint state and is released with the
write.

Admission runs in one fixed order, the one Product and Issue writes already use. `create` and `reopen`
are the two transitions into `open` and both run it, on that same staged-intent journal:

1. take the admission lock;
2. under it, settle the request id first. A committed or staged intent of the same request id comes back
   as it is, before any check of live state, so a repeat that overlaps the request it repeats is replayed
   rather than refused as a conflict with the sprint it opened itself. The same request id carrying a
   different payload is refused as `validation` before any side effect;
3. check product, issues, registry and both conflict rules only for a fresh request. A staged intent is
   resumed on the state it was admitted on, so a Product or Issue that changed after the refusal does not
   turn a repeat into a validation error;
4. apply the backend steps through the staged intent. Each one recognises what an earlier attempt of the
   same request already did. A metadata answer other than `True` is a backend refusal, not a success:
   the call reports `audit_pending` instead of `created` or `reopened`, the staged intent stays and the
   retry with the same request id finishes that same operation;
5. commit one audit event, however often the delivery repeats.

The sprint reference is written last, and writing it is what publishes the sprint: a row on the sprint
board counts as a sprint only once it carries one. An interrupted create is therefore never observed as
an open sprint without its product, issues and reservations.

An unfinished create holds nothing. Instead of holding the installation it compensates: a step refused
after the row was created takes that row back, so no unreferenced row is left behind, and only when the
backend also refuses that does the row stay for the repair to pick up. Before it publishes, a resumed
create or reopen re-checks both conflict rules; if another sprint took the slot or the project meanwhile, the
repeat is refused as `sprint_conflict` or `resource_conflict` naming that sprint and publishes nothing.
That refusal is only the answer once the request holds nothing: if the row cannot be taken back, or a
refused `reopen` cannot write its observer preimage back, the caller is told `audit_pending` instead and
retries under the same request id until the cleanup goes through.
The losing request is then filed again as a fresh one. Like a staged Product or Issue write, an
unfinished sprint create blocks the checkpoint until it is retried or dropped.

Sprints created before ownership existed carry none of these fields. They stay readable, exportable and
restorable exactly as they are, and nothing fills the fields in for them: `show`, `status`, the board
export and the checkpoint record leave the three fields out rather than answering `""` and `[]`. `reopen` re-checks every rule
above, so such a sprint is refused with a message that names what it lacks; the supported move is to open
a new sprint that owns its issues. `reopen` is refused the same way when the sprint's own issues have
since been closed or its projects are held elsewhere.

`sprint close` freezes the active cards linked to that sprint. It archives its terminal Done tasks with
the normal task archive audit, leaves linked non-terminal cards on the board, and returns both lists.
The Done transition clears the completed worker claim and its resolved routing fields, so that stale
ownership does not prevent normal terminal archival; `archive` still refuses a live claim.
Cards without that `sprint_ref` are not considered. Product and Issue records are never closure targets,
including if malformed metadata links one to the sprint, so an Issue remains open until the PO calls
`issue close`. The close request is staged: retrying the same request id after a lost archive or status
reply resumes the same task set, does not archive a task twice, and records one sprint close event.
Legacy sprints without reservations are closed without retroactively archiving cards.

Installation config may set `sprint_budget.signal` and `sprint_budget.hard`; defaults are 3 and 6. The
schema resolves omitted values to those defaults before rejecting a hard limit below the signal limit.
Each charge is a `budget_recorded` audit event; the charge that stops a sprint is paired with a
`budget_hard_stopped` event carrying the hard limit and the triggering card-event identity. `show`
returns the thresholds and `signal_reached`/`hard_reached` with the totals. The signal appears in a newly
launched observer prompt but does not stop work. At the hard limit the dispatcher marks the sprint
`stopped`, stops its observer and skips new linked claims; active cards continue their normal cycle. Only
`sprint reopen --role po` clears the stop.

`sprint resume` accepts JSON with required string fields `selected_step`, `selected_why`,
`rejected_alternatives`, `current_task`, `dod_state` and `next_safe_step`. It is stored separately from
normal comments and carries a `[sprint:resume]` marker. The entry is a concise semantic delta, not a copy of
machine-derived delivery, CI or board telemetry. `show` and `status` compute freshness only from semantic
observer work: a card entering Assessment, Blocked or Done; a budget event; or a PO comment on the
sprint. Claims, reports, Validate moves, reviewer launches, routing and observer-authored events do not make a
resume stale. Missing data is `resume_missing`; a semantic transition may trail its resume for up to five
minutes, then is `resume_stale`. That comparison belongs to an open sprint. A closed or stopped sprint
takes no further semantic work — it accepts no resume, comment or current task — so the record on its row
is the last one anybody wrote, and freshness for it is read from that record alone: no later event ages it
and no status path reads the audit for it. `reopen` puts the sprint back under the ordinary comparison.
A sprint summary therefore reads the committed audit at most once per operation, and an installation whose
sprints have all finished does not read it at all. Neither command reads an observer transcript. The dispatcher records a
durable delivery batch before it wakes or replaces an observer, coalesces pending semantic events to one
high-water mark, and owns all waiting for workers, reviewers and CI. An observer acknowledges it by passing
the matching `--delivery-id` and `--through-event` from `status` to `sprint resume`; those values are audit payload,
not part of the six stored resume fields. `secretary status --json` exposes the
same entity-derived state for every sprint in `installation.sprints.items`, including stopped status and
its reason, budget, resume freshness and observer state. If the live board cannot be read, that fact is
reported in `installation.sprints.error`.

Wakes are intentionally sparse: only the semantic card edges (Assessment, Blocked and Done), an eligible
human control-plane return to Issues, sprint budget events and PO sprint comments open observer work.
Claims, routing, reports, validation telemetry and observer-authored writes do not. Prompt acceptance is
out of band: delivery records only that the pane took the prompt, and the next ordinary reconciliation
reads one durable audit snapshot to close the batch only on the matching resume. Delivery therefore never
polls the board or calls an observer-facing `Monitor` command. Legacy broad cursors are narrowed without
discarding still-unacknowledged semantic work.

An Assessment entry is one decision visit. The first observer `task decide` is canonical for that visit;
a redelivered observer turn repeating the same kind returns that decision without adding another comment,
and a different kind is refused until the card enters Assessment again. This protects the release/rework
seam from delivery retries without weakening RED or Blocked classification. The complete decision
transaction is serialized per card, including the fresh state read, visit resolution and staged board/audit
write; two observer processes cannot each make a different decision for the same Assessment visit.

### The open-sprint limit

How many sprints an installation may hold open at once is the instance setting `open_sprint_limit`, an
integer that is either 1 or 2. Absent means 1, which is the shipped default; an installation may
explicitly enable 2 for the gated pilot. Nothing enables the pilot by itself. A value the setting cannot honour
(3, a string, `true`, anything the schema refuses) fails closed to 1 rather than raising, so a malformed
setting can never widen the limit and can never stop admission either; `validate_instance` reports it as
an `open_sprint_limit` finding, because failing closed is otherwise silent to the operator who wrote it.
The limit is read from the installation config at the moment admission asks for it, so changing it needs
no restart and an installation whose config cannot be read answers 1.

What admission checks, in which order, and at which limit is stated here and nowhere else; every other
passage in these docs points here rather than repeating it. Each check is a read of live state, and all
of them run before the first board write. In the order admission applies them:

1. **disjoint project reservations**, at either limit. A project another open sprint already reserves is
   refused, naming the project and its holder. This rule predates the limit and reads the same at 1 and
   at 2.
2. **a different product**, at limit 2 only. Two open sprints may not share the owning Product. A sprint
   that declares no product at all, a row from before sprints owned one, cannot be proven disjoint and is
   refused, whichever of the two it is: the candidate is judged on its own value first, so the answer
   does not depend on which row was looked at first.
3. **non-overlapping canonical repository roots**, at limit 2 only. Roots are compared as absolute
   resolved paths, and overlap includes nesting: two spellings of one working tree are one working tree,
   and a root that contains another's is the same tree twice. A stored root that is not already absolute
   is refused on either side rather than resolved at check time, because resolving it would answer
   against the working directory of whichever process happens to run admission. The candidate's own roots
   are judged before any pairwise comparison and whether or not another sprint is open, so a one-row open
   set published by restore or by a reopen cannot carry a root the next check would read as a different
   tree.
4. **the count**, at either limit: the installation already holds as many open sprints as it may.

The count is last because it names every open sprint and distinguishes none of them, so a caller who acts
on it can close the wrong one and come back for a second refusal. A resource refusal names the sprint
holding the resource, and closing that one sprint both frees a slot and clears the collision. Every
collision above the count is therefore reported before it, including when the installation is already at
its limit.

At limit 1, where checks 2 and 3 do not run, admission reads exactly as it did before the pilot: a project
another open sprint reserves is refused first, and everything else is refused on the count.

`create`, `reopen` and restore are held to the same invariants, under the same
`sprints/admission.lock`. `restore_create` is deliberately not an admission decision, since it reproduces
exported rows one after another, so recovery judges the exported open sprints as a *set* instead: once,
before the first backend write of either set, by admitting the rows one at a time in reference order
against the rows already accepted. An archive is therefore not a way to arrive at a pair admission would
have refused. The limit applied is the target installation's, not the source's, and the whole restore runs
inside the admission lock so a `create` cannot slip between recovery's check and its write.

### The declared observer

A sprint carries exactly one observer value in `sprint_observer`. There is no dynamic default, no
value inherited from `role_defaults.observer`, no missing-field fallback and no permanent
tri-state. Four tagged forms exist, and only the first two are executable:

| form | meaning |
| --- | --- |
| `{"kind": "head", "profile": "claude-observer"}` | the sprint is observed by that one head profile |
| `{"kind": "none"}` | the sprint runs without an observer |
| `{"kind": "historical", "profile": HEAD, "source": "observer_lifecycle_audit", "event_id": EVT}` | a closed row whose head was recovered from durable lifecycle events when the field was introduced |
| `{"kind": "historical", "profile": null, "source": "migration_unknown"}` | a closed row that never launched an observer, so there is nothing honest to recover |

A `historical` value is provenance of what ran, never a declaration of what to run. An open sprint
carrying one is corrupt in exactly the way a missing value is.

`create` and `reopen` both require `--observer`, spelled either `none` or one head profile from
`heads.yaml`. Absent, null, empty, `default` and `inherited` are not interpretations this model has.
A named profile is resolved against this installation's head snapshot — the same registry the
dispatcher launches from — at every boundary that can put one on a row: create, reopen, and the
restore preflight. A sprint is never opened, reopened or republished on a head that does not exist,
because the fence would stop its projects on the first tick and the operator would be reading a
critical outcome instead of a validation error. Registry drift *after* the
declaration is what the fence is for.
`reopen` writes the fresh choice while the sprint is still closed and only then changes its status,
so the row is never readable open under a value the reopening caller did not choose; `create` writes
it with the rest of the fields, before the reference publishes the row.

The reader is strict everywhere and always: an open sprint whose observer metadata is missing,
unreadable, historical, or names a profile the registry does not have is corrupt. It is not launched
from `role_defaults.observer`, its cards do not move, and it does not silently become observer-free.

Restore validates the whole exported set before the first backend write of any set, cards included,
and refuses rather than publishing part of it. Every exported row must carry a value, closed rows
too. A row without one is named and refused, and the refusal does not guess why: an export can lack
the field because it is damaged or because it was taken before the field existed, and nothing in the
archive tells the two apart. The repair is the same either way — declare the value on that row in
the export's `state/board/sprints.json` and restore again. So is the repair for an open row whose
declared head has left the registry.

### The observer fence

The production tick checks every open sprint's observer before it reconciles records, advances
active cards or claims Ready. The check is pure: it reads the sprint board, the head registry and
the observer records, and launches nothing.

A sprint is fenced when its declared head has not been launched, is dead, does not match the running
record, is parked behind a failed bring-up, or when its declaration is corrupt. Fencing is
project-local: it excludes that sprint's reserved projects and linked cards from reconciliation,
active advancement and Ready claims, and leaves every other project running. `{"kind": "none"}`
passes with no launch and no probe.

A sprint board that cannot be read is fenced, not waved through. The sprint board and the Pipeline
board are separate Kanboard projects and fail separately, so the tick can read a sprint's cards
while its declaration is unreadable, and advancing them would be moving cards whose observer nobody
could check. Each successful pass records every open sprint's reservations in the production state;
a pass that cannot read the board fences from that snapshot, plus every card whose own metadata
names a sprint. Cards belonging to no sprint keep running. A sprint admitted since that last
successful pass is in neither source's reach: the snapshot predates its reservations, so a card
sitting in a project it reserves without naming the sprint itself is fenced by neither and can
advance or be claimed while the board is down. That window runs from the admission to the next pass
that reads the sprint board.

The fence writes one durable `observer_fence_raised` event with `outcome: critical` per reason, and
clears with `observer_fence_cleared` once adoption is confirmed: a record for that sprint naming
exactly the declared profile, with a pid on disk that is alive. A pid that has not been written yet
does not clear it. The lifecycle grace window that reads an unwritten pid as alive exists to decide
whether to relaunch a head, and a head can die before it ever reaches the observer prompt; releasing
another role's cards needs the stronger proof. Clearing is therefore normally a later tick's: the
launch happens after the fence in the same tick and the pid lands after that.

A fence that cannot be evaluated ends the tick. If the check raises — most plausibly because its
critical outcome cannot be staged on a full or unwritable volume — the tick returns
`observer-fence-unavailable` and runs no reconciliation, no advancement, no budget accounting, no
observer reconciliation and no Ready claim. An empty fence is not "nothing was decided", it is
"everything may move", and the sprint whose outcome could not be written is exactly the one whose
cards must not.

`task create --sprint` records the sprint reference in Pipeline-card metadata. `task show` and
`task list` expose it as `sprint`, and `task list --sprint` filters by it. `sprint show` derives its
`cards` list from that live card metadata rather than storing a duplicate list. New links and comments
are refused after a sprint is closed. `current-task` additionally requires that the selected card already
carries this sprint reference.

An open sprint holds every project in its `reservations`: only its observer may create a card in such a
project, and
only with `--sprint` naming that sprint. Observer and dispatcher may move and edit linked cards, so the
ordinary claim, report and review cycle gains no extra step. The PO may create, move or edit only with an
explicit `--sprint-override` plus a non-empty `--sprint-override-reason-file`; the reason text is stored
as its own field in the durable audit. Without the flag the PO gets `sprint_write_forbidden`, as do retro,
steward and every other role. The refusal names the holding sprint and asks the caller to write through
its entity. The refusal itself is audited as `sprint_guard_denied` and is not duplicated when the same
request id is retried.

A write of role `observer` is authenticated against the sprint it names before any of that. The
dispatcher launches a head for one sprint and binds `SECRETARY_OBSERVER_SPRINT` and
`SECRETARY_OBSERVER_GENERATION` into that head's own command line; `runtime.env` cannot supply or
replace them. A card linked to another sprint, and a `sprint resume` or `sprint current-task` naming
another entity, are refused as `observer_sprint_mismatch`; a head that carries no binding is refused as
`observer_identity_unbound` on its first write, because a caller that cannot name its sprint cannot
be placed at all. Both refusals are audited as `sprint_guard_denied` with that code, which is how an
identity failure reads apart from a role that has no such right.

The check keys off the declared role, and the caller declares it. `sprint close`, `sprint reopen` and
the budget write take role `po` and no binding, and each takes the sprint reference as an argument, so a
head declaring `--role po` reaches any open sprint rather than only its own. The observer's own close of
its sprint is that call. Nothing below the CLI distinguishes it from the PO making the same call, and the
audit shows it as `role=po` with the observer's actor id.

The index of the projects open sprints reserve is kept locally next to the audit log, keyed by project id.
An index written in an older key space is rebuilt from the sprints board before it answers. For a project
outside any open
sprint it triggers no read of the sprints board. For a write into a held project the sprint is re-read
live: an unreachable board returns `sprint_guard_unavailable` rather than allowing the write. Closing or
stopping a sprint releases the hold.

Sprint mutations share the board event log and pending-audit recovery with card mutations. They carry the
sprint reference as `ref`, and a repeated `--request-id` returns the committed event without recording a
second one.

Every write command passes role guards and transition checks. A mutation first receives an append-only
pending audit event, is then checked against the live board, and only then counts as committed. An
unresolved pending write blocks a consistent export and the recovery checkpoint until `reconcile-audit`.

A `--request-id` is an ownership claim over the operation it recorded, not only a de-duplication key for
the append. A retry under an id the audit already holds, committed or still staged, is answered from that
record only when the caller means the same operation: the same event kind, the same card, and the same
request. Every task write is compared this way, `create`, `comment`, `report`, `verdict`, `decide`,
`claim`, `move`, `edit`, `archive`, `routing` and the restore writes alike. For a report the comparison
covers the marker, the body digest and the classification, so a second `report --kind done` on one card
under the previous round's id and with a new body is refused with error code `validation` and exit code 2
instead of being answered with the old event: no second audit event, no second comment, and no success
reported for a report the board never received.

Three fields are left out of that comparison, and only because a retry after the write cannot recompute
them: the column a `move` left, the digests of the text an `edit` overwrote, and the body a restored
comment carries until it is confirmed on the card. Each describes the state the write itself replaced, so
comparing it would refuse ordinary retries. Everything the caller asked for is compared.

Every task write result carries `replayed`. It is `false` when this call performed the write, and `true`
when it answered from an event an earlier call under the same id had already committed or staged, so a
caller distinguishes an accepted write from a replay without parsing prose.

`report --kind done` checks `git status --porcelain` of the worker's workspace before writing anything and
refuses with `uncommitted` if there are uncommitted changes: the worker fixes that in its own session
instead of learning about it later from a blocked card. An untracked runtime tail is not counted as dirt,
`--kind blocked` is not gated because work in progress is legitimate there, and the dispatcher's after-the-
fact check stays as defence in depth.

`report --kind blocked` also requires `--classification`, one of `external_fact` when the blocker is a fact
outside the card that somebody has to change first, or `wrong_task_definition` when the card itself is wrong.
The two are repaired by different people in different places, so the worker's own view of which one it hit is
what the observer starts its analysis from. The value goes into the `reported` audit payload as
`classification`, and into the report comment as a `classification:` line under the marker, so it is readable
without parsing the report prose. Both are written by the one backend write the report already makes; the
classification is deliberately not card metadata, because a second write that can fail on its own would leave
a card field that silently disagrees with the audit. `--kind done` takes no classification and is refused if
given one. An observer moving a card out of Blocked must give a non-empty reason, the same requirement the
steward carries moving one into it; the reason is a comment on the card and its digest is in the `moved`
event, so how a Blocked card was disposed of stays answerable.

The `reported` events are the authoritative copy and keep the classification of every block, so counting how
often one head blocks is a question for the audit. The compatibility CLI (`triggered_agents pipeline report`)
cannot write a classification, so it refuses `--kind blocked` outright and names this command instead. The
vocabulary has one definition, in `secretary.tasks`. Its `--kind done` is unchanged.

The dispatcher also remembers the SHA that a mechanical gate or a red review rejected in the current
attempt. A `done` report on the same SHA does not move to Validate: the first such report sends the worker
back to rework in the same workspace, requiring a new commit. The second moves the card to Blocked so the
rework loop cannot spin forever. If the code deliberately does not change, for instance when the defect is
in a test or in the gate itself, the worker reports `--kind blocked` with the analysis instead of another
`done`.

The audit trail is always written to the installation's data directory: `--data-dir`, else
`SECRETARY_DATA_DIR`, else `data_dir` from instance config. A relative `data_dir` resolves against the
instance file, not the working directory, so a call from another project's workspace does not leave a data
directory there. If the data directory cannot be resolved, the command fails with a usage error rather than
writing next to the process.

### The no-observer ceiling

A card whose sprint declares a concrete observer is bounded by that observer's judgement: the
verdict parks and a person decides how many rounds it is worth. A card with no observer (no
sprint, a sprint that declares `none`, a closed sprint, a sprint board that cannot be read) has
nobody to say stop, so a ceiling on substantive red reviews says it instead.

The count is the card's own `review:red` comments, so it needs no sprint to live in, survives
anything the dispatcher forgets, and is idempotent without bookkeeping: a retried verdict write
dedupes on its request id and leaves no second comment. A red mechanical gate and a red CI rollup
leave dispatcher comments with no verdict marker and are not counted, the same separation the
sprint budget makes between `red_review` and `red_ci`. The third red review moves the card to
Blocked with a reason naming the ceiling instead of opening another worker round. Only the
terminals are stopped: the workspace and the branch stay as the round left them, and coming back
is one explicit transition. Three is early on purpose. Blocking a card nobody is watching is a
question to a person, not lost work, and it is cheaper than letting the card eat five rounds
unattended.

The ceiling does not bind a card that parks for a decision. The observer is the ceiling there, and
a counter that fired would be deciding a card the observer is still holding.

The other half of the rule, that a dead head on an unobserved card gets one replacement per
attempt and the second death blocks, is not implemented. A dead head is answered by the wait
watchdog's own per-kind respawn ceiling, on an unobserved card as on any other.

### Routing telemetry per attempt

A card does not keep routing history: the resolved review head is cleared when it leaves Validate, and the
whole routing block is reset on a return to Ready. So "who was the worker and who was the reviewer on
attempt N" lives only in the append-only journal, as `kind: "routing"` events. The dispatcher writes them
without touching the backend: the event has no mutation, only a record written through the normal
pending/commit path, idempotent by request id.

An attempt (round) is one worker launch plus the review it earned. A claim opens attempt 1; each bounce
back to rework (a red verdict, a red gate) opens the next. Respawn, resume after a pause and a restart
after a rejected SHA stay inside their attempt. A return to Ready followed by a new claim adds an attempt
rather than overwriting the previous one: the number comes from the journal, not from dispatcher state, so
it survives both a lost record and a restore.

A return to Ready counts as such in both forms: an operator retry of an already blocked card, and an
ordinary preempt or requeue of a live card from in-progress or validate. The dispatcher issues the attempt
a new attempt id at that moment, otherwise a repeat claim would land on an already committed claim request
id, return the old event and leave the card in Ready. The previous attempt's heads are stopped, because the
new round enters the same workspace.

### The report generation

A worker round is identified by a report generation: a counter in the dispatcher's own record that
starts at 1 when the card is claimed and advances by one whenever a new report round opens, whether
that is a red mechanical gate, a red review or a done report bounced back at an already-rejected
checkout. It never repeats within an attempt and never goes backwards. A respawn inside a round does
not advance it: the head that died never reported, and the round is still waiting for that report.
It advances once for each such round however many ticks the round takes to open: a red transition
reserves the round's generation with the intent it writes before moving the board, and the tick that
finishes the transition assigns that reservation rather than computing a new number, so a recovery
that re-enters the completion does not spend a second generation on one round.

The generation is the round key of the report identity. It is the suffix of the `done` and `blocked`
request ids in `TASK.md`, and of the report body path those commands name; the two block
classifications get an id each, because a request id claims its payload and a block restated under
the other classification is a different report. One value serves all of them: the generation is
persisted before any document names it, the `TASK.md` a head is given is written from it, and the
prompt that wakes a retained worker names it and says which suffix the round's commands carry. The
number is there so a command replayed out of the retained conversation is visibly the wrong one to
the agent and to a human reading the pane before the call is made.

What such a replay does is worth being exact about, because the id alone does not stop it. A request
id is an ownership claim over its payload: a stale command carrying a new body is refused with
`validation` and exit code 2, but an identical retry is a retry, and the protocol answers it from
its committed event with `replayed: true` while the board gains no marker. So the round's body files
go when the next round opens, all of them, the round about to start included; a replayed command
that reads one of them fails on its first step. What it cannot do is refuse a worker that writes the
old path again with the same contents, which the protocol answers as the retry it looks like. So the
guarantee here is exactly this and no more: a command from a round that is over never records a
report of the current round, and it can still answer its caller with a success that belongs to the
round it came from. Refusing that call outright means authorising the attempt's open generation
inside the report protocol, which is a durable protocol change with its own compatibility promise
for stale retries, and it is not part of this contour.

What ends the round instead is the dispatcher, which is the only place that knows which round is
open, and it ends it on two facts of its own. The first is which report belongs to the round. The
marker on the card cannot say: it is `[report:done]` whoever filed it and for whichever round. The
request id can, and the audit keeps it beside the marker, so a report is attributed to a round by
the id its command carried and by nothing else. That id is the round's identity in full, attempt
and generation both, so an id from a round that is over names that round, an id from an earlier
attempt on the same card names that attempt, and an id a head invented for itself names nothing.
None of them ends the round that is open, and all of them leave the card exactly where it was,
which is the second fact: a head that has stopped working with its round unreported is pointed at
the current command once and then the card is blocked. That covers a stale call as it covers a call
never made, whatever the head did or did not run, because both leave the same nothing behind. The
mechanics of that wait are in `docs/OPERATIONS.md`.

Which ids a round issued is read from the checkout first and from the dispatcher's own state only
as a fallback, and for the same reason the generation is: the document is what the live worker is
holding, and a record that was lost and re-adopted carries a fresh attempt id while that worker
keeps reporting through the commands it was given. Only ids that name the open generation are taken
from the document, so a checkout a round behind cannot hand back the previous round's ids. When the
checkout cannot be read at all, the ids are the ones the dispatcher would issue itself from the
record's attempt and the open generation, which is what makes a live worker holding older ids get
bounced once and re-materialised on the same round.

The document answers through a record of the dispatcher's own, never by scanning it for report
commands. Every worker `TASK.md` ends with a hidden `<!-- report-round generation=N ids=... -->`
line carrying that round's request ids base64-encoded, on the same terms as the decision record
below it: written last, after every section a card description or an observer decision is rendered
into, matched as a whole line, and the last such line in the file taken. A card description is
arbitrary Markdown that is copied into the document unchanged, so a `--request-id` token in ordinary
prose is indistinguishable from a rendered command; reading the commands would make such a token an
id "this round issued" and let a report committed under it end a round the dispatcher never handed
it to. The generation the checkout names is read from the same record, and the same reasoning
applies to it.

Only a committed audit event ends a round. A write stages its event before it touches the backend,
so a staged `reported` event is a report that may never reach the card, and a tick that consumed one
would end the round on a call that had not happened. The other side of that window is a report whose
comment landed and whose audit append then failed: the round stays open until the audit is repaired,
which is what retrying the same command does, and until then the wait is a wait like any other.

Reading the round out of the audit rather than off the board is what keeps this inside the
dispatcher. Nothing is added to the report protocol: the same call from the same worker is still
accepted, still idempotent under its own id, and still answers a same-payload retry with the
original event.

It is dispatcher state, so a dispatcher that lost its record recovers it: the `TASK.md` in the
checkout names the round its live worker is working from, and the rounds already reported and
consumed are the floor when no document can be read. Both are lower bounds and the larger one wins,
so a recovered generation may skip a number but cannot reuse one. Only consumed reports count in
that floor. A report still waiting to be read belongs to the round that is running, so counting it
would hand the adopted record a generation that no report on the board names, and the report that
is already there could never end its round.

The comment index a new report marker is scanned against is a separate value and stays a comment
count. A generation that skips or lags the card's comments does not blind the dispatcher to a fresh
report, and `review_baseline` is likewise only the comment index the next review verdict is read
from and the round key of the reviewer's own verdict identity.

### The observer decision a rework round is opened on

A round opened by a `rework` decision is opened on that decision, and the worker of the round is
handed it. The decision text is frozen where and when the round's generation is: written into the
red transition's intent before the board moves, assigned to the record when the transition
completes, and rendered into every `TASK.md` that round produces, the replacement head's and the
retained head's alike. It is never looked up again at document-build time. "The most recent
`decision:rework` comment on the card" is a different question, and a decision recorded after the
round opened would answer it and silently replace the instruction the round is running under.

In the document the decision comes first, under a heading that names it as the instruction to
follow, and the reviewer's red body is kept below it as the context it was decided on. Where the two
disagree the document says the decision wins, so a decision that accepts some findings and rejects
others reads as exactly that. The prompt that wakes a retained worker names the decision as the
authoritative instruction rather than only pointing at the file, because a conversation that is only
sent back to a document ranks its sections itself.

Nothing inherits: every round that opens carries the decision that opened it and no other, so the
value is written wherever a round is opened. The red transition assigns it unconditionally, and the
stale-done bounce clears it in the same mutation that advances the generation. A round opened by a
red mechanical gate or by a bounced done report therefore carries no decision, its document reads as
it did before this existed, and it never inherits the adjudication of a review it has nothing to do
with. A round opened with no decision behaves throughout as it always did.

Like the generation, this is dispatcher state that a lost record recovers from the checkout, so an
adopted card reads back what its live worker was told to follow instead of consulting the card's
newer comments. Every worker `TASK.md` ends with a hidden
`<!-- observer-decision generation=N body=... -->` line carrying the round's decision base64-encoded,
empty body included when the round has none, and the recovery reads the last such line in the file.
Descriptions and decisions are both arbitrary Markdown, so neither the delimiters nor the field can
be anything either of them may contain: an encoded field has no character that ends it, and the
dispatcher's own line comes after every section they are rendered into.

### Worker retention through validation and review

After a worker reports `done`, the dispatcher suspends its live, addressable worker session before
moving the card to Validate. A head with no pane handle is not retained: nothing can address it,
so it is stopped with a confirmed stop instead. The record carries the retained state through the mechanical
gate and through the review that follows it, so the worker cannot change the checkout while either
is judging it. Before the reviewer starts, the dispatcher confirms that suspension from the head's
heartbeat; a session it cannot confirm gets a confirmed stop, and the round loses its continuation.
Nothing here stops every terminal in the worktree, so the worker's own pane stays the reviewer's
split anchor.

Both red verdicts return the card to In progress through one transition, and both hand the round
back to the session that wrote the code. What differs is what opens it. A red mechanical gate opens
it directly, always: nothing about a failed gate is a judgement anyone has to make. A red review on
a card whose sprint declares a concrete observer opens nothing by itself; the card parks in
Assessment once the reviewer's stop is confirmed, and the transition runs on the tick that performs
a recorded `rework` decision. A red review on a card with no observer to decide opens it directly
after that same confirmed stop, up to the no-observer ceiling above, which blocks the card instead
of opening the round. Nothing else moves a card to In progress for rework, and the
transition always runs the same order, differing only in the phase it records:

1. The red intent, with its phase, the baseline of the report it closes, the generation it reserves
   for the round it opens, the observer decision opening it if there is one, and the reason the card
   is moving, goes to disk.
2. The card moves.
3. The reserved generation and that decision become the record's, and are persisted.
4. The delivery decision is made: a confirmed-suspended session takes the continuation, and
   anything else gets a confirmed stop and exactly one replacement. Either way the head is given a
   `TASK.md` written for the new generation before it is woken or launched.

Whether a session is held changes only the last step. A round with nothing to reuse opens the same
durable intent, because it is the round whose replacement a crash would otherwise lose: the record
would still name the report that closed the round, and the next tick would replay that report as a
new completion while the card sat In progress with no worker. A tick that dies anywhere after step 1
is recovered by re-entering this transition against the board as it stands, never by replaying the
Validate handoff. An open intent outranks everything else the card could be doing: every tick
finishes it before it reads the mechanical gate, a report marker or a review verdict, and before it
starts a reviewer. The intent is immutable once written and carries its own reason, so a rollup that
has turned green between two ticks cannot retract a red round the card is already owed, and the card
moves once however many ticks it takes to finish the transition. The suspension of the session about to be reused is confirmed from the heartbeat
immediately before the delivery boundary opens, on recovery as well as on the first attempt, rather
than trusting the confirmation the reviewer launch made earlier; a session that is no longer
confirmably suspended is stopped with a confirmed stop and replaced exactly once. The dispatcher then
updates `TASK.md` with the failure and the round's report identity, persists a pending-delivery
boundary before SIGCONT, then checkpoints confirmation only after the provider durably records the
continuation user turn. Terminal activity is a recovery hint for records without that boundary, not
the delivery proof. Recovery after a crash cannot mistake the previous `done` report for a new
completion, replay an incomplete delivery as if it were confirmed, or overwrite a confirmed
continuation. A checkpointed delivery is finished on the next tick, so the rework opens its own
round and the reuse is recorded on the card once and only once. A pending delivery whose head is
awake again by the next tick is not typed into a second time: it fails the confirmation and takes
the confirmed stop and the single replacement, and the host keeps its own guard against re-sending
over a turn already underway. All supported Codex and Claude workers are interactive and accept a
continuation. Legacy records that name Codex exec are normalized to TUI before launch or rejected
by registry validation, so no one-shot exec worker can reach this branch. The
routing record and card comment name the outcome as a reused continuation or a replacement, with
the worker profile, model, effort, reason and timestamp. A dead session, an unavailable
continuation transport, or a lost handle is an explicit fallback: the dispatcher confirms the old
worker has stopped, writes a durable launch intent, and starts exactly one replacement. That intent
is where the transition changes hands, so it changes hands only once the write succeeds: an intent
the state plane refuses leaves the red transition on the record, and the next tick finishes it and
starts that one replacement. Retention
and stop signal the head's private process group, so its helpers are frozen too. An unconfirmed
stop never permits a second writer in the workspace.

Retention is scoped to one round: the report that opened it, the gate and the review that judge it,
the park the verdict opens, and the decision that hands that round back. Nothing else keeps a worker
session. A preempt or requeue back to Ready, a `report:blocked`, a move to Blocked and a
reconciliation onto another card all stop the worker head and clear the retained state, so the next
round starts from a replacement head. A preempt is an instruction to end the current attempt, not to pause it. A green review ends
the round too: the merge tears the worktree down, waking the suspended head before it is killed.

```json
{"kind": "routing", "ref": "PROJECT-N", "payload": {
  "attempt": 2, "attempt_id": "...", "phase": "verdict", "outcome": "red",
  "heads": [{"role": "worker", "head": "codex", "head_source": "card",
             "adapter": "codex", "model": "gpt-5.6-terra", "model_source": "profile",
             "effort": "default", "codex_mode": "tui",
             "resource": "openai-sub", "account": "openai-subscription"}]}}
```

`phase` is `worker` (worker launch), `review` (reviewer launch) or `verdict` (the attempt's outcome,
carrying both heads), so worker/reviewer pairs group by outcome without a join. A verdict `outcome` is
`green` or `red` from the reviewer; a mechanical-gate bounce closes the attempt with its own value
(`gate_red`, `merge-gate_red`, `review-freeze_red`) so a return to rework is not attributed to whoever
reviewed it. If the reviewer already returned green and the merge gate then bounced the card, both events
stay in the journal.

The decision is made once, at claim time, and there is no substitution at launch: the head that starts is
the head the claim decided. That decision reads the card override or `role_defaults` and then resource
health, and it may end somewhere else than it started. A preferred head whose resource is red or spent is
replaced by the first launchable head along the fallback chain the registry writes for it, breadth-first,
cycles read once. Only that chain: nothing is inferred, and a chain entry the registry no longer describes
is dropped rather than launched, because an unreadable profile has no resource to probe and its readiness
reads `unknown`, which is launch-allowed. So a record carries one head per role plus `head_source`, saying
where its id came from: `card`, `role_default`, `fallback` (the claim walked the chain), or `record` (the
head pinned in the card's dispatcher record when it was claimed earlier).

Two answers end that walk without a claim, and both leave the card in Ready with the reason on the tick,
naming the dead resource and its probe verdict. Nothing launchable anywhere in the chain is one: a head
started into a spent subscription costs an attempt, a watchdog respawn and a round, and a card waiting in
Ready costs nothing. The other is a transfer that would hand the worker and the reviewer the same head.
A review is worth having because someone other than the worker reads the work, so a failover that removes
that is refused rather than performed; two roles pointed at one head by the registry itself is an
installation's own decision and is claimed as before.

Both are claim-skips, and a claim-skip is a statement about one card. The Ready pass records it and
considers the next card, so an unclaimable card never stops work that has somewhere to go. Every kind of
claim-skip is named in one set the pass reads, rather than compared against by hand: a skip missing from
that set does not degrade the pass, it ends it, and the cards behind the skipped one are not considered
that tick or any following one while the resource stays dead.

A head reached by failover is never a silent substitution. The claim writes the pair onto the card as
`resolved_worker_head` / `resolved_review_head`, adds one comment naming the head, the preference it
replaced and the resource verdict that caused it, reports both in the tick, and the reviewer's document
says which head wrote the branch when it is not the one the card asks for.

Because the decision is made once, the attempt keeps it. A dispatcher that lost its record takes the head
pair from the card's own resolved worker and reviewer fields when adopting, rather than resolving the
override and `role_defaults` again: otherwise a role default changed mid-attempt would hand the review to a
different head and the journal would honestly record a head nobody claimed the attempt with. If the head
pinned at claim time has disappeared from the registry, nothing is launched: the card moves to Blocked with
the reason that the claimed head is unavailable, the dispatcher record is dropped, and nothing is appended
to the journal. Substituting the current `role_defaults` would be exactly the launch-time swap the
installation does not have, so the decision is left to a person.

A profile name is not a historical key: several profiles can be one model at different effort levels, a
profile may pin no model at all, and profiles get repinned. So each head carries its full launch
configuration, captured at launch and never re-read from the registry. The bring-up itself takes the
snapshot and the dispatcher writes it to the journal as is. The registry is re-read only for an adopted
card whose launch happened in a previous life of the dispatcher.

`model_source` says where the model came from, and `model` is empty only when the source says so
explicitly. A profile with no model launches its CLI without a model flag and the CLI picks one; at launch
the same sources are read in the same order the CLI uses. If the model is pinned nowhere, the value stays
empty under a `cli_default` source, meaning "chosen by the runtime" rather than a silent omission. The
launch record rejects an empty model under any other source.

Those sources are read from the environment the head will actually get, not the dispatcher's own. A head
command goes through the role-environment wrapper, which drops every `runtime.env` variable outside the
role allowlist, so the snapshot reads the role launch environment. Otherwise the journal would record a
model that never reached the CLI.

Every launch inside an attempt writes its own event: respawn after silence, restart after a pause, rework.
The request id includes a digest of the configuration, so relaunching the same head commits once, while a
launch on a different adapter, model, effort or resource adds a second event and replaces the attempt's
active head. The verdict always carries the head that earned it.

The reading side is `secretary.routing_journal.attempts(events, ref)`: the sequence of attempts for a
finished card, with heads and outcome. These events go into the recovery checkpoint with the rest of the
event log and are restored on materialise.

## Production dispatcher

The production runtime runs as a single tick or a continuous loop:

```bash
python3 -P -m secretary dispatcher production-tick --instance INSTANCE
python3 -P -m secretary dispatcher production-observe --instance INSTANCE
python3 -P -m secretary dispatcher production-run --instance INSTANCE
```

The systemd timer uses the one-shot `production-tick`. The runtime handles only supported task
transitions, persists claim and review state, and checks the live board before recovery. The production
owner is recorded in dispatcher state; an owner mismatch, a dirty workspace, a missing report or an
unresolved audit state stops a transition instead of falling back silently.

## Pause

The pause is shared across the pipeline and sits on top of the product dispatcher:

```bash
python3 -P -m secretary pause drain|freeze --instance INSTANCE --reason "why"
python3 -P -m secretary resume --instance INSTANCE
python3 -P -m secretary pause-status --instance INSTANCE
```

`drain` stops claiming new cards and dispatching background roles, but cards already running finish their
cycle. `freeze` additionally stops live worker and reviewer heads (a stop, never a teardown) and freezes
the tick entirely: nothing advances and no watchdog fires on a head that was stopped on purpose. `resume`
brings stopped heads back up in the same workspaces, hands a card whose report already arrived to the next
tick, and restarts the watchdog windows.

The flag is `<data_dir>/dispatcher/pause.json`, read by every `production-tick`. Background roles read a
mirror flag, written and cleared by the same command.

During a freeze an operator can exclude their own workspace with `--exclude-workspace`; the manual archive
command uses this to freeze the pipeline from inside a worker.

A freeze set by an automation on the configured allowlist expires after a configurable TTL (45 minutes by
default): the tick checks this before skipping on freeze and lifts the pause through the ordinary `resume`
under the same tick lock. A freeze set by a person holds until an explicit `resume`. A frozen tick moves no
cards but still writes and pushes the checkpoint.

## Connecting a project

The current low-level onboarding has these stages:

```bash
python3 -P -m secretary project add ...
python3 -P -m secretary project provision-start ...
python3 -P -m secretary project provision-apply ...
python3 -P -m secretary project gate ...
```

A project's identity is set once by the top-level binding: `id`, `repo`, `adapter`, `default_branch`. The
binding's mutable `plane`, `policy` and `remote` fields are not part of identity and are carried over into
the rewritten binding by a repeat `project add`, as is `orca_binding`. The scanner and provisioning prepare
changes but do not enable a binding. Enabling is allowed only through a passing gate tied to verified
revisions, a provision run and a write set. A higher-level resumable workflow is a roadmap milestone.

An enabled binding is never rewritten by an ordinary `project add`. Re-onboarding one is an explicit
operator request, `project add --re-onboard`, which disables the binding and drops its canonical adapter in
the same transition that publishes the new draft, so the project holds no executable adapter until a new
gate passes. The binding is the last file the transition writes, because it is what the next run reads to
decide whether a takedown is still owed: an interrupted re-onboarding stays visible as the enabled binding
it started from, and a retry carries it through. It does not enable anything and grants the scanner and the
provision agent nothing.

A takedown opens a new onboarding cycle. The draft records it as `onboarding_cycle` and the provision run
id derives from it, so provision results and gate receipts from an earlier cycle cannot be reused on an
unchanged scanner head. Evidence is bound to the cycle that produced it.

Diagnosing failures, recovering a stale disabled draft, re-onboarding an enabled legacy project and
verifying a passed result are described in
[Operations](OPERATIONS.md#connecting-a-project-gate-and-stale-input-recovery).

## Memory

Facts are stored flat as `memory/facts/global/<slug>.md` or `memory/facts/<project-dir>/<slug>.md`. One
fact is one distilled markdown record. The curator is the writer role; every other agent reads through
`memory_search`, `memory_get` and `memory_list`.

```bash
python3 -P -m secretary memory verify --instance INSTANCE
python3 -P -m secretary memory propose --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md
python3 -P -m secretary memory commit --instance INSTANCE --actor ACTOR --propose-id ID
python3 -P -m secretary memory supersede --instance INSTANCE --actor ACTOR \
  --scope SCOPE --slug SLUG --file FACT.md --supersedes OLD-ID
python3 -P -m secretary memory reindex --instance INSTANCE
```

Writer operations require an actor and go through the journal protocol; direct edits bypass the audit
trail. `reindex` changes only the derived index and must not overlap another index writer. Model and
dimension come from instance configuration.

## Knowledge

Long recoverable documents (brainstorms, decision logs, incident write-ups) live in
`state/knowledge/<section>/<document>.md` for the installation itself, and in
`state/knowledge/projects/<project id>/<section>/<document>.md` for a connected project. How this
differs from curated memory and the board is described in
[Architecture](ARCHITECTURE.md#knowledge-planes).

```bash
python3 -P -m secretary knowledge write --instance INSTANCE --actor ACTOR \
  --path decisions/2026-07-25-sprint-1.md --file DOC.md
python3 -P -m secretary knowledge write --instance INSTANCE --actor ACTOR \
  --path projects/codegen-orchestrator/brainstorms/qa-node.md --file DOC.md
python3 -P -m secretary knowledge list --instance INSTANCE
```

Path segments are ASCII: letters, digits, `.`, `_` and `-`. A document imported from elsewhere under
a non-ASCII filename is renamed on the way in.

`write` replaces a document wholesale and commits only `state/knowledge` under the shared writer lock, so
no manual `git commit` is needed and it does not race the tick writer. A document containing a secret is
rejected with code 2 and nothing reaches disk. Rewriting identical content reports `changed: false` and
makes no commit.

## Secrets

```bash
python3 -P -m secretary secret init --instance INSTANCE
python3 -P -m secretary secret set --instance INSTANCE --id ID --scope SCOPE --purpose PURPOSE \
  --stdin [--environment VAR] [--materialize runtime-env|file [--materialize-path PATH]]
python3 -P -m secretary secret list --instance INSTANCE
python3 -P -m secretary secret import --instance INSTANCE --file ENV_FILE --scope SCOPE \
  --purpose PURPOSE [--materialize runtime-env|file [--materialize-path PATH]]
python3 -P -m secretary secret remove --instance INSTANCE --id ID
python3 -P -m secretary secret materialize --instance INSTANCE [--target runtime-env|file]
```

A secret value never travels through argv: `set` reads it from stdin or `--file`, and `import` takes a
`KEY=VALUE` env file (LF-separated, no comments or blank lines, one secret per variable). No command prints
a value: `list` returns catalog metadata only, and `import` and `materialize` print ids and variable names.
Reading a value stays an internal API until there is a safe consumer for it.

`secret init` is interactive by design. It refuses to run when stdin or stderr is not a terminal, and makes
that check before generating the recovery phrase rather than only before printing it, so the phrase cannot
reach a pipe, a file or a log. The phrase is printed once to stderr, the operator confirms they wrote it
down, screen and scrollback are cleared, and only then does `init` ask for a few words of the phrase back
before creating the store.

Layout of `secrets/` in the instance repository:

```text
secrets/
  catalog.yaml            open metadata: id, scope, purpose, materialize — tracked in Git
  installation-key.json   open KDF parameters and verifier for the installation key — tracked in Git
  values/<id>.enc.json    one encrypted envelope per secret — tracked in Git
  installation.key        the raw installation key, 0600, outside Git (.gitignore)
```

The store is the fourth writer of the instance repository, next to board/runs, memory and knowledge:
`init`, `set`, `import` and `remove` take the same repository lock and commit their own pathspec in a
single commit, so the catalog and the values it names cannot diverge in history. `list` takes no lock and
commits nothing. `materialize` takes the lock too, so it does not cross a writer mid-read, but it writes
only the materialised files outside `secrets/` and makes no commit: materialisation targets are not part of
the instance repository. The open part passes the same redaction gate as `state/`: a secret accidentally
pasted into a `purpose` field stops the write instead of reaching a commit. The encrypted envelope does not
go through that scan, because its body is ciphertext plus open decryption parameters.

Recovery is described in [Recovery](RECOVERY.md#secrets). With the recovery phrase the installation key is
rebuilt and materialisation targets are rewritten from the catalog. Without the phrase everything
non-secret is restored, and `recover` prints a `locked`/`missing` report and writes nothing: `locked` means
the value is encrypted but the key is absent, `missing` means the catalog names a secret whose envelope is
not in the repository.

The installation key belongs to the installation user, the same user that owns the host and the
installation, not to a narrower role. The store does not promise worker isolation: it has no broker and no
grants, and the installation key opens every secret at once, with the same rights that previously read
`runtime.env`.

Data-plane, archive-restore and unit runbooks are in [Operations](OPERATIONS.md).
