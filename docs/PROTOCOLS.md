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

## Codex provider-internal fan-out policy

Codex fan-out is a best-effort operational preference, not a lifecycle or security boundary. Every
worker, reviewer and observer launch uses the strongest validated low-fan-out CLI configuration and
receives an explicit instruction to perform its turn in the current head without spawning or
delegating to children. A rare provider-internal child is acceptable product behaviour.

The capability evidence is in
[Codex provider-internal fan-out capability evidence](evidence/codex-provider-fanout-2026-08-13.md).
For the installed 0.147.0 CLI it records no provable native boundary: `--disable multi_agent` still
produced a real collaboration call, while the globally configured v2 wait-disabled row had no
collaboration call but no schema evidence. The artifact records strict-config rejection and
ignored-role state as typed evidence, so neither a rejected candidate nor a silently ignored role
can pass as isolation. Consequently the product describes its launch policy as practical
suppression, never as capability isolation.

`triggered_agents.runtime.codex_preflight` remains the one pre-pane preparation boundary. Its v1
record preserves `schema_absent`, `schema_unknown`, `allowed`, `unknown` and `violation` as honest
diagnostics, but none of those fan-out states permits or refuses a pane. Workspace trust is the hard
pre-pane requirement. Current 0.147.0 launches proceed with `schema_absent`, an unbound structured
journal source where available, and the explicit low-fan-out launch configuration.

Provider-edge collection is bound to that same run. It appends one
of `collaboration_call`, `child_thread_edge`, `unknown_thread_edge` or
`unparseable_provider_event`, with parent and child thread identities when present, tool name when
known, SHA-256 raw-event digest, source sequence/location and capture time. A collaboration call or
non-empty child edge is a violation. An unknown tool or relation, missing expected parent,
malformed event or failed event write is unknown. These states are telemetry only: they never stop
or replace the HeadRun, block a card or sprint, refuse prompt delivery, or affect continuation
liveness. Telemetry loss is also non-fatal.

The live canary measures practical suppression. The configured run should normally produce no child
edge, but an observed edge is recorded and the run continues.

The concrete Codex source is its structured session-event JSONL, not a pane read and not the
tolerant workspace-level session liveness lookup. The pre-pane attestation stores the v1 source
root and the set of journal paths that existed before the pane. The collector reads the journal's
`session_meta` and `event_msg` envelopes, never pane text. The retained TUI collaboration shape is
`event_msg.payload.item.type = CollabAgentToolCall`, with `tool`, `sender_thread_id` and
`receiver_thread_ids`; the documented `collab_tool_call` shape is also normalized. An unfamiliar
collaboration-shaped item is `unknown`, never ordinary output. Before the first prompt, exactly one
newly created journal for that workspace must supply one session identity. An explicit parent
`thread.started` is preferred; Codex 0.147 journals which omit it use the same selected
`session_meta` id as the parent/root identity. That path and identity, plus durable first/root/last record anchors and a
digest of the initially observed range, are written onto the same `HeadRun` with a zero cursor anchored
to the first raw record. The one scanner then classifies the complete selected source range from that
first record, through the selected root, and through every already-present tail line before delivery.
Source selection does not exempt a session preamble or any pre-root record. Ordinary records may
durably advance the cursor. A malformed, collaboration, child-edge, unknown-relation or cursor-write
failure becomes typed diagnostic evidence where writable and never gates a prompt. Recovery reopens
the same path where available and verifies its
complete initial range, session id, workspace, parent identity and prior cursor before reading a later
line. Missing, unreadable, changed or ambiguous source evidence is non-fatal `unknown` telemetry.

### Post-delivery HeadRun handoff

There is one authoritative `HeadRun` after a launch delivery. The writer order is fixed: construct
and validate the exact run; persist its handleless preflight identity in the role launch intent;
create and bind the pane; persist the rebound handle and leaf; bind and persist the Codex source
when applicable; capture that post-delivery run; then write routing, role state and clear the
intent. `head_ops.spawn` returns the captured run, and worker, reviewer and observer launchers,
intent confirmation and adoption consume that value rather than a pre-delivery local copy.

The provider callback owns source facts. A later launcher or lifecycle writer may add only the pane
address it proved and its own forward lifecycle evidence. It cannot remove a bound source, move a
cursor backwards, replace a bound session/range, or replace run id, spec, workspace, task, role or
pid identity. A conflicting, stale, malformed or foreign candidate is an identity fence: it is not
adopted, resumed, signalled, stopped, replaced or attributed. The same merge is used by worker,
reviewer and observer recovery, so a retained continuation and an observer watchdog read the exact
source delivery committed. Source binding is observational for lifecycle purposes and never grants
fan-out telemetry the authority to change that lifecycle.

Observer event delivery has one narrow exception to screen-advisory liveness: when a retained Codex
observer HeadRun carries its v1 source descriptor, the dispatcher persists a versioned
`wake_liveness` episode before it interprets `tui-idle`. The episode names that exact run id and
HeadRun fingerprint, source fingerprint and opaque cursor, first observation, last admitted
progress, no-progress rung and terminal outcome. A new admitted cursor keeps the same head and
event batch and resets only that batch's no-progress ladder. Missing, malformed, incomplete and
foreign source evidence is typed unavailable or identity-mismatch; it cannot refresh, reset or
rebind an episode. A bound unavailable episode retains its binding and observation across dispatcher
reload; without an admitted baseline, a later cursor cannot create one for that same batch. This is
provider-progress liveness only. Fan-out events remain telemetry and
have no stop, delivery, replacement, cleanup or blocking authority.
At an identity-fenced observer replacement, the old episode is first terminalized and retained as
audit evidence; the replacement launch intent then opens a fresh episode bound only to the new
HeadRun while carrying the unchanged delivery id and event high-water mark.

## Receipt names

The protocol names exactly two receipts:

- A worker-local broad receipt is owned by the worker. It attests one local broad suite's result
  for the current content, stays in that worker's workspace and never travels downstream.
- A dispatcher-owned exact-SHA gate receipt is owned by the dispatcher. It attests completed
  terminal gate checks for one exact SHA and lifecycle stage, then travels with the active card to
  review and Assessment and into the release audit.

### Dispatcher-owned exact-SHA gate receipt

Every real mechanical gate result materializes a dispatcher-owned exact-SHA gate receipt bound to
one checkout and containing:
`validated_sha`, `base_sha`, `gate_mode`, terminal `required_checks` (name, conclusion and URL),
`completed_at`, and a `command_or_check_set_digest`. For a local gate this is the configured
command's digest; for GitHub it is a stable required-check-set identity, not a workflow-definition
or run digest.
Both object IDs are full 40-character SHA-1 or 64-character SHA-256 values; abbreviations are not
evidence. A local gate captures HEAD before and after its command and fails closed if the command
moves it. Only `local` and `github` may carry receipts; `none` and noop are valid only without one,
and an unknown mode is never accepted.
The dispatcher persists the dispatcher-owned exact-SHA gate receipt with the active card, renders it
into the reviewer's task document, replaces it with the fresh post-review receipt in the Assessment
delivery, and writes the fresh final receipt into the release audit after the mandatory exact-SHA
pre-merge re-check. That document is written outside the checkout, under the run artifacts and
private to the installation, because a workspace's identity is the tracked diff and untracked files
a receipt hashes; the pane
receives only a bounded pointer to it. `TASK.md` is likewise a generated, git-ignored workspace
handoff packet. These are operational projections, not repository documentation or candidate
changes. A receipt is evidence, not permission to skip the pre-merge check or independent review.

## The pull request a GitHub gate opens

A `github` gate opens the pull request the `pull_request` workflow needs, and that pull request is
also the only description of the change a later reader of `main` gets: its title lands in the merge
commit. Both title and body are built deterministically from the board — no model is asked anything
on this path. The title is `<ref>: <card title>`. The body names the card and the branch it merges
into, quotes the card's statement, and carries the worker's own account of the round from its
`report:done` comment; each source is bounded, redacted like any other board excerpt, and simply
omitted when the gate runs before it exists. The gate re-runs on every later tick and once more
before the merge, and each run brings an already-open pull request up to the better description it
can now build.

What bounds that is the dispatcher's own record, never the pull request's text. When the gate
opens or edits a pull request and the backend accepts the write, it records on the card's
dispatcher record which pull request it wrote and a digest of the exact title and body it sent.
A later tick may rewrite that pull request only while that record exists, names that pull request,
and still describes what GitHub returns. Everything else is somebody's writing and is left alone
for good: a pull request a person opened (an empty description is the ordinary case of this — a
body is optional on GitHub, and emptiness is not evidence of authorship), one whose body or title
was edited after the gate wrote it, one opened before this record existed, and every pull request
belonging to a card whose record was lost to a restore, a reinstallation or a re-adoption from the
board. A lost record costs a description that stops being refreshed; the opposite default would
cost somebody their words. Text that already matches the record is not re-sent, so a repeat tick
on unchanged data makes no call at all.

Refreshing is scoped, by decision, to the pull requests the gate recorded writing. A pull request
opened before this record existed keeps the fixed stub the old gate gave it — it is not read, not
edited, and updated by hand if anyone cares. There is no migration, no recognition of the legacy
stub text and no operator override, because each of those would infer authorship from something
other than the gate's own accepted write.

Authorship is deliberately not readable out of the pull request: a body is supplied by whoever
edited it last, so no marker, digest or phrasing inside it can say who wrote the text around it.
The boundary this does not defend is a forgery by someone who can already write to the
installation's own state — anyone who can edit the dispatcher's production state can claim any
pull request. Write access to the repository does not confer that.
The description is not a condition on the code:
a backend that refuses the update leaves the pull request as it is and the gate's verdict unchanged,
while a pull request that cannot be opened at all is still a gate failure, because without it the
project's CI never runs.

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
the card holds its green dispatcher-owned exact-SHA gate receipt, its candidate SHA, its report
round and request ids and its suspended worker session, and the next tick launches the reviewer again
against that same evidence. It does not move through Ready, does not launch a worker, does not re-run
the mechanical gate or any broad validation,
does not regenerate the candidate, and charges the sprint no budget event; each attempt is one
`review-infrastructure-retry` action naming the held candidate. The retries are bounded
(`SECRETARY_REVIEW_INFRA_RETRY_ATTEMPTS`, ten by default) and count consecutive failures only. Once
the ceiling is reached the card moves to Blocked with a reason that names the infrastructure, the
untouched receipt and the candidate SHA, so recovery is another reviewer launch rather than a new
worker round over the same code. An inventory that will not answer is
different: it cannot prove whether a reviewer is already live, so it preserves launch ambiguity
and retries the inventory without launching another head or consuming the headless-failure ceiling. Those
retries and the block that ends them are classified by the one bring-up vocabulary
[below](#bring-up-outcomes): the retry ceiling is spent before the outcome is written, and spending
it produces an infrastructure outcome over the held candidate, never a verdict about the card.

When a reviewer pane and heartbeat already exist but its document nudge receives typed `busy`
evidence before any send, that is a pending delivery rather than a started review. The launch
intent retains the exact reviewer HeadRun, handle/leaf binding and workspace with a capped durable
retry schedule. Until a later nudge confirms delivery, recovery does not freeze or signal the
worker, write reviewer routing or lifecycle attribution, clear the intent, or replace the pane.
Successful confirmation crosses the ordinary launch adoption boundary once; `unavailable`, malformed
and stale-handle evidence remain separately typed and use their existing conservative recovery paths.

### Broad-check handling

Workers use focused checks while developing and run no more than one local broad suite for a report
generation/unchanged SHA unless they state why it was rerun. That broad run goes through
`secretary check broad`, which streams the combined output, returns the check's own exit status and
writes a worker-local broad receipt under the ignored `state/checks/` path: command and check-set
digest, cwd and imported project provenance, start/end/duration, exit code, parsed verdict and
counts where the runner prints them (scanned off the stream, so cleanup output after a summary
cannot erase it), and a bounded diagnostic tail. The worker-local broad receipt is evidence about
content, not time: it records the content as one git tree object id, the tree this worktree with
tracked edits and untracked files would commit to. `secretary check show` therefore answers whether
it still describes the code in front of the role, and committing that content unchanged keeps the
worker-local broad receipt usable. While a usable worker-local broad receipt exists, rerunning the
broad suite only because the pane scrolled its output away is prohibited; an edited worktree or a
concrete red result being fixed opens a justified new run, named in the report. A worker-local broad
receipt only ever claims an import it observed from the process that ran the check. The `--module`
shape runs the suite itself and records what that process imported. An arbitrary
`--command` shell may change directory or import environment before any interpreter starts, so it
attests no import and is never reused in place of a run. Reuse asks more than that the import was
observed: it has to have resolved inside the candidate workspace, so a legitimate import environment
that resolves the project to another checkout is recorded truthfully and still refused. One
predicate answers that question for every route, and a check is keyed by its structured check set —
shape, module and exact argument vector — so two invocations that render alike cannot answer for
each other.
Anything less than an intact, finished receipt with observed provenance for exactly this content — a
truncated or edited artifact, a result no run could have written, a killed or timed-out run, a
checkout with no resolvable identity — is not a summary and does not attest anything. The
worker-local broad receipt never leaves the workspace and is never committed: only an executed
local/GitHub gate with a valid dispatcher-owned exact-SHA gate receipt is authoritative reusable
evidence downstream. A none/noop gate or missing dispatcher-owned exact-SHA gate receipt attests no
broad suite, so the role runs or requests validation appropriate to the decision. Reviewers
independently inspect changed code and invariants, but do not repeat an attested broad command on
the same SHA without a recorded `rerun_reason`; targeted reproduction remains appropriate for a new
blocker, uncovered external behaviour, or security/data-loss risk. Re-review
packets carry the previous reviewed SHA, previous blocker text/IDs, current SHA and changed-path
delta, so the next reviewer verifies the delta and closure rather than restarting at the original base.

Before a card is given to a worker at all, the dispatcher asks whether the registered project's
broad-check contract can attest that project, through the same implementation the worker's own
`secretary check broad --module` resolves through, so a card is never issued on a contract the
worker would then refuse. The question costs a read of the project binding and of the adapter
beside it: no workspace, no head, no process. The answer is one of three named states, and every
caller branches on them by name, with no default branch that lets an unrecognised answer through as
permission:

- `fit` — the contract is declared usably, and the card is issued as always;
- `refused(shape)`, one of five enumerated shapes — `adapter_unavailable`, `adapter_invalid`,
  `broad_check_incomplete`, `interpreter_unavailable`, `cannot_attest_project`. A refusal always
  wins, and always before the card is put in work: no workspace, no head, no round spent. The card
  is blocked through the bring-up vocabulary [below](#bring-up-outcomes), because an installation
  whose registry cannot supply a usable contract is a failure of the host and not a verdict about
  the card, so it carries the infrastructure class, the `contract-preflight-infrastructure-blocked`
  action token and the refusal shape as its evidence;
- `undecidable(question)`, one of three enumerated questions — `relative_interpreter`,
  `no_registered_project`, `project_unavailable`. The card goes to work.

The two boundaries around that are deliberate. The preflight answers for the *declared* contract
only. The adapter schema resolves a relative interpreter against the candidate workspace, and at
preflight there is no candidate workspace, so the question belongs to the side that will hold that
tree and comes back as `relative_interpreter` rather than being answered against the registered
checkout, which is a different directory. `undecidable` therefore resolves in favour of
compatibility rather than of saving the round: it is a named decision to let the card through,
carrying its own evidence, not the absence of an answer. A relative interpreter is the documented
and recommended spelling, and breaking that published promise to make an internal check convenient
would move the product contract the wrong way.

The second boundary is that a declared contract is executed as declared, not checked against a
layout heuristic. The adapter states which interpreter runs the check and which package that run
must import; the preflight asks only whether that statement is complete and whether the interpreter
it names can be started. What such a run actually imported is caught afterwards, by the receipt's
own import provenance. The one contract judged against a checkout's layout is the legacy default
that an adapter declaring nothing falls back to: it names Secretary's own package for every
registered project alike, so for a checkout that does not hold those sources it buys a check of an
installed copy of somebody else's code, and that is the `cannot_attest_project` refusal.

Observers consume the worker report, reviewer verdict and dispatcher-owned exact-SHA gate receipt
before code/CI exploration. A valid executed dispatcher-owned exact-SHA gate receipt suppresses its
routine broad rerun; none/noop or missing evidence does not, and permits appropriate focused or
broad validation. Contradictory evidence, RED/Blocked classification, a real Definition-of-Done gap,
or a security/data-loss concern also requires research. The role that owns a further broad rerun
records its reason in the report; a worker-local broad receipt never transfers ownership of an unexplained
rerun or suppresses a targeted reproduction of a new concrete risk.

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
model, this one. There is no parallel `triggered_agents pipeline` writer: steward report cards,
steward signals, and retro Done retention enter through Secretary's canonical TaskReader/TaskWriter
adapters, preserving the audit and sprint guards. `--target` is accepted as a second spelling of
`--to` on that command; both name the same destination state.

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
high-water mark across both open and closed cards. Whichever way the reference is arrived at, the
board is asked whether that exact reference is claimed before it is written, and a claimed one is
refused: an allocation is only as free as the enumeration it was counted from, and an archived card
holds its reference for good. Allocation, staging and `createTask` are one
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
history available through `issue show --ref` and checkpoint recovery. `sprint close` closes an issue
through this same lifecycle when its decisions file gives one of those four verdicts, with the same role
and the same reasons; it never closes an issue because a sprint that declared it ended, and it never
records an issue somebody else closed as a verdict of its own. `issue list --closed` includes
both open and closed issues; without it the list contains only open issues.

A Product and an Issue are not execution tasks and never enter the execution columns: `move` and `claim`
both reject one before any write, whatever column it currently sits in. Work on an issue is a separate
card the PO creates in Ready.

A record belongs to its product, so every Product and Issue row is created in the lane of that product:
the active swimlane whose name is exactly the product id, created on demand when the board has none. An
Issue takes it from `issue_product`, a Product from its own id. Nothing about the board takes part in the
choice - not the order of the lanes, not which lane is first, not whether a `Default swimlane` exists -
so every writer, every retry and every restore of the same record choose the same lane. The project
bindings of a product take no part either, which is why a product bound to several projects is not
ambiguous: the lane is named after the product, never after one of its projects, and it never becomes a
second source of truth about what a record belongs to. Execution cards are unaffected: they stay in the
lane of their project.

The rule binds every write, but not the rows that predate it, and a restore puts a record back into the
lane its checkpoint recorded, so a board can hold rows the rule would place elsewhere. Their supported
repair is one idempotent command, which plans by default and writes only when told to:

```bash
python3 -P -m secretary product reconcile-lanes
python3 -P -m secretary product reconcile-lanes --apply
```

The plan writes nothing: it reports every row that is out of place with the lane it sits in and the lane
it belongs in, the lanes that have to be created, and a per-product summary. `--apply` performs exactly
those moves. The destination is the same `product_swimlane_id` every writer uses, so the command has no
second opinion about where a record belongs. A move is one `moveTaskPosition` into another lane of the
same column, so the row keeps its reference, metadata, comments, column and open or closed state, and the
rows that travel together arrive after whatever the destination lane already holds, in the order the plan
lists them. A run on a board already in order moves nothing and says so, and a run interrupted halfway is
simply continued by the next one, which re-reads the board and plans only what is still out of place.
Closed records are not moved, only counted, and a record whose product is unstated or is not a registered
Product is never guessed at: it is listed in the output so a human decides what it belongs to. A row that
is both is both: it is counted among the closed records whether or not its product can be resolved, and
still listed as unresolved, because the closed count is the visible half of that decision and must not
depend on anyone being able to tell what the row belongs to.

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

`transaction list` includes typed Product/Issue pending events as read-only entries with their request id,
event kind and subject ref, as well as released transaction documents. `retry` finishes the staged operation
exactly where it stopped and commits its one audit event; a request already committed is answered with its
record. `discard` drops a released transaction only after reading the board: a create whose row exists and a
priority or close change whose board comment exists are refused as `live_write` and have to be retried instead.
It never discards a typed pending event, which is also refused as `live_write` and repaired by retry. `adopt`
files a released transaction document that lives outside the journal back under its own request id, which is
how a document carried out of the journal comes back into `retry` or `discard`. The commands cover Product
and Issue writes alike; the journal is one.

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
python3 -P -m secretary sprint close --role po --ref sprint:ID --decisions-file DECISIONS.yaml
```

Stored fields are the goal, the Definition of Done text, repositories, the owning product, its issues,
the reserved projects, open/closed/stopped status, the declared observer, a
budget counter by event type, the current card and a structured resume entry. The six valid budget event
types are `red_review`, `blocked`, `red_ci`, `preempt`, `recreated_task` and `hotfix`. Production derives
them from durable card audit events: a red review, a move to Blocked, a red mechanical gate, a preempt of
an active card back to Ready, or a tagged recreation or hotfix creation. The card-event id becomes the
budget request id, so a repeated tick cannot charge it twice. Green cards and observer activity have no
matching event and do not move the counter. Beside those six there is one recorded type that is
never charged: a bring-up that never produced a head is counted as `infrastructure_blocked`, in its
own stored field, so that an observer can see how often the host failed to bring a card up while
counting it can never move a threshold. See [Bring-up outcomes](#bring-up-outcomes).

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

A sprint reference passed by the caller is used as given; without one, `sprint:N` is allocated from
the sprint board's own high-water mark over open and archived rows, by the same rule and with the
same claim check as a card, and is remembered so a repeat of a stalled create writes the reference
it was already going to write. It is never derived from the row's Kanboard identifier: row ids and
references are separate sequences, and a sprint numbered after its row takes a reference the board
handed out long before.

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
the normal task archive audit and returns that list.
The Done transition clears the completed worker claim and its resolved routing fields, so that stale
ownership does not prevent normal terminal archival; `archive` still refuses a live claim.
Cards without that `sprint_ref` are not considered. Product and Issue records are never closure targets,
including if malformed metadata links one to the sprint, whatever the close was told to decide: an Issue
is closed only through the Issue lifecycle and never archived as a card. The close request is staged:
retrying the same request id after a lost archive, issue close, disposition or status reply resumes the
same task set, repeats none of those writes and records one sprint close event; a retry that states other
decisions is refused rather than answered with the staged ones.
Legacy sprints without reservations are closed without retroactively archiving cards, and they declare
no issues, so they need no decisions either.

### The decisions a close carries

A close states what became of every issue the sprint declared and of every card it still holds in a
working state. Neither follows from the close: a sprint may close with its Definition of Done only
partly reached, so a closed sprint is not a done issue, and a card left in Ready under a closed contract
is not a disposition. Both are stated by the closing PO in one file:

```yaml
issues:
  - ref: issue:e6e8c24e9de7a7cad54b
    verdict: resolved          # resolved | invalid | duplicate | wont_do close the issue; open keeps it
    reason: the nudge fix landed in this sprint
  - ref: issue:32a78b7822bb013ef99a
    verdict: already_closed    # somebody else closed it; name what they closed it as
    actual: duplicate
    reason: another PO closed it as a duplicate while this sprint ran
cards:
  - ref: secretary-1400
    verdict: drop              # done | drop
    reason: superseded by the next sprint's cut
  - ref: secretary-1401
    verdict: already_moved     # somebody else took it there; name the state it is in
    actual: ready
    reason: its own head put it back in Ready before the close got to it
```

Both sections are optional in the file and neither is optional in the close. Every declared issue needs
a verdict and every card that is not Done needs a disposition; a close short of one is refused with
`validation` before the transaction is opened, naming the issues without a decision and the cards with
their states, and writing nothing at all. An unknown ref, a ref decided twice, an unknown verdict, an
empty reason, an unknown field or section, a key that is not a name at all (`1: x`, which YAML reads as
an integer key) and an unparsable file are refused the same way. `actual` is required by the two
confirmations below and refused on every other decision.

A closing verdict closes the issue through the same lifecycle as `issue close`, with that reason and no
new role. `open` writes nothing to the issue: the sprint's close event carries the basis, which is where
the audit keeps the reason an issue was allowed to outlive its sprint.

A disposition ends with the card archived, and what the board records before that is the verdict: `done`
moves it to Done, `drop` moves it through Ready, which is the released edge that releases a retained
worker — a card still holding a claim cannot be archived. Both moves carry the disposition's reason as
the card's comment, and the archive carries it again. A card whose dispatcher work is still live is not
disposable at all: the close refuses with `live_work` and names it, and the head is settled first.

`closed` is published as the last step of the close. The terminal phase runs in one order — the verdicts
on the declared issues, the archival of the Done cards, the dispositions, then the status, then the
reserved-project index and the completion of the transaction — and an interrupted close therefore leaves
the sprint open. That is what keeps a successor out of an unfinished close: an open sprint still reserves
its projects, and `create` already refuses a second sprint on a reserved project, so the retry of the
close finishes its dispositions before any successor can be opened. `close` also takes the admission lock
(`sprints/admission.lock`) that `create` and `reopen` take, so a concurrent create waits for the answer
rather than racing the status it depends on; the invariant does not depend on how long that lock is held.
Because a disposition then moves a card of a sprint that is still open, and the reservation guard refuses
a PO move into an open sprint's project, the close carries the guard's own `sprint_override` with the
reason `disposed by the close of <sprint-ref>: <the disposition's reason>`. There is no other way around
the guard.

A close that has performed a step is never thrown away. A terminal refusal discards the staged close only
while nothing has been written; from the first issue write onwards — the marker is durable before that
write, as it is before the status change — the refusal is `audit_pending` and the staged plan stays on
the record, and a retry that states other decisions is still refused.

No step of a close can be left unresolvable, and no step ends on anything but its own committed event.
Every step — an issue verdict, an archival, a disposition's move and its archival — runs under a request
id derived from the close's, and that id is the only proof the step happened. A pending event under it is
a step whose backend effect landed and whose journal write did not: the close drives the same id again
and does not finish while it is still pending. The state an object is observed in is never proof: a card
sitting in the state a disposition wanted is as easily somebody else's move as this close's. Whether a
disposition needs its move at all is read from the state this close froze into its plan, not from the
board as it stands now.

An object that changed under a close, with no committed step of this close's to account for it, is
somebody else's change, and it is settled by an explicit decision rather than adopted:

- **Before any write.** The preflight reads every declared issue. A closing verdict for an issue somebody
  else has already closed is refused with `validation`, naming each ref and the reason it carries, before
  the transaction is opened; so is a decision to leave such an issue open. The closer rewrites the file,
  confirming what happened with `already_closed` and naming that reason in `actual`. A confirmation of an
  issue that is open, or one naming a reason the issue does not carry, is refused the same way. The set of
  issue *close reasons* is unchanged; what gains a value is the set of decisions the file may state.
- **In flight, once the preflight has passed and writes have begun.** The close neither finishes nor
  invents a verdict: it stops with `close_conflict`, naming the ref, the stated verdict and the fact that
  actually holds, and records the conflict on its own transaction. The retry of the same request id may
  amend exactly those refs, to exactly the confirmation of what happened (`already_closed` with the
  issue's reason, `already_moved` with the card's state), and nothing else; every other change is refused
  as a restated file, as before. So the step stays resolvable, and the closer resolves it.

A confirmation writes nothing to the object it confirms. `already_closed` leaves the issue with the reason
it carries; `already_moved` skips the disposition's move and archives the card where it stands, and it is
accepted only for the states a disposition of this close would have produced (`done`, `ready`), so a
confirmation cannot take a card off the contract while it is still in a working state. Both are recorded
in the close event with their prose, which is where the audit keeps why the sprint accepted them.

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
its entity.

Both answers the guard can give are audited, and neither is duplicated when the same request id is
retried. A refusal is a `sprint_guard_denied` record; a granted override is a `sprint_guard_override`
record carrying the project, the holding sprint, the override reason and the request id of the operation
it authorized. Each is written under its own derived request id, so the operation keeps its own retry key,
and the grant is written before that operation stages or effects anything: an override that could not be
recorded does not happen. The grant is a control-plane record about authority, deliberately separate from
the operation's own record — a migrated Card transition's typed event describes the lifecycle edge, not
who was permitted to make it — which is what keeps the answer to "who overrode this sprint's reservation,
and why" in one shape across `move`, `create` and `edit`.

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

A Card state change is the one mutation that has moved to the board protocol. `move` and `claim` run
through the board host, which records each occurrence as a typed protocol event in the same
`events.ndjson`: `record_type` is `board.protocol_event`, the lifecycle edge is the object
`transition: {source, target}`, and the reason is carried as text rather than as a digest. Released
generic `moved` and `claimed` rows stay readable exactly as they were written, and every other operation
(`report`, `verdict`, `decide`, `routing`, comments, create/edit/archive) keeps its generic record. A
reader that cares about card transitions therefore has to handle both shapes explicitly; `record_type` is
what tells them apart. A request id written before the migration also keeps the operation it named: a
retry of a released generic `move` or `claim` id replays that record and finishes its released cleanup,
rather than being read as a typed request it never was.

One staged occurrence has exactly one owner of its outcome, `MutationEventTransaction`, and one window in
which its record may be discarded: the call that issues the single column operation, and nothing else.
Everything before that call — reading the card, re-authorizing the edge against the live state — is inside
the window, because none of it has touched the board. Everything after it is fail-closed. That includes
the read that confirms the move: a read which times out, or which finds another writer's column, says
nothing about whether this move was applied, so it keeps the exact pending typed record rather than
reporting the card as never moved. The same holds for the follow-up board work and for the event commit.

That board work is inside the same transaction, not after it. The claim metadata a `claim` writes, the
routing and retry fields a move into Ready or Done resets, the review head a move out of Validate clears
and the move's reason comment all run once the column effect is confirmed and before the event commits.
So a refused or failed column move leaves nothing behind at all - no claimed-but-Ready card, no event -
and a follow-up that does not land keeps the exact pending typed record instead of reporting a clean
journal over a half-written card.

Recovery is likewise split. A pending typed transition is repaired by re-reading the card and committing
its exact event only when the requested target is live on the board. It never repeats a column move, and
it refuses an effect that is unproven, contradicted or gone, leaving the pending record for an operator
instead of publishing a transition that never happened. Where the outstanding board work is recomputable
from the card - the Ready and Done reset - recovery finishes it first and refuses to close the record
while it remains incomplete, so a transient backend fault resolves on the next `reconcile-audit` rather
than stranding claim metadata under a clean audit. A claim's own metadata is not recomputable: the worker
id and the resolved heads are dispatcher decisions no reader of the card can reconstruct. Refusing there
would make a proven start permanently unrecoverable, so recovery publishes the occurrence it proved and
leaves the missing claim to the dispatcher's live claim check, which reports it as a controlled
divergence instead of launching on it. Retrying the same request id is the repair that also restores the
metadata, and only while the record is still pending: it repeats no admission check and no column move,
finishes the metadata and then publishes. Once `reconcile-audit` has published the occurrence, that retry
answers from the committed event and writes nothing further, so the claim gap is the dispatcher's to
report from there on.

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
steward carries moving one into it; the reason is a comment on the card and is carried by the card's
transition event, so how a Blocked card was disposed of stays answerable.

The `reported` events are the authoritative copy and keep the classification of every block, so counting how
often one head blocks is a question for the audit. The retired `triggered_agents pipeline` CLI is no longer a
write path; the vocabulary has one definition, in `secretary.tasks`. Its `--kind done` is unchanged.

The dispatcher also remembers the SHA that a mechanical gate or a red review rejected in the current
attempt. A `done` report on the same SHA normally does not move to Validate: the first such report sends the
worker back to rework in the same workspace, requiring a new commit. The second moves the card to Blocked so
the rework loop cannot spin forever. The exception is a red GitHub gate classified from its failed
job and step as an enumerated infrastructure failure: action-download HTTP 5xx, unavailable image
registry or Buildx registry setup, or an unavailable runner during `Set up job`. Classification
reads only the failed `gh run view --log-failed` fragment: action download requires its runner
notice adjacent to a failed-download 5xx entry; registry failures require a container/Buildx step
and either a named registry outage or a Docker daemon HTTP 5xx; runner failures require a
runner-service diagnostic. That exact SHA stays in Validate for an automatic gate retry and opens
no worker round. The gate asks GitHub to rerun the failed Actions run, then treats the rollup as
pending until that run has a new terminal state; rereading a concluded failure is not a retry.
Reruns are SHA-scoped and bounded by `SECRETARY_GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS` (two by
default). The rerun request itself uses the normal `SECRETARY_GATE_TRANSPORT_MAX_ATTEMPTS`
transport ceiling. An exhausted ceiling, or a run GitHub cannot rerun, moves the card to Blocked
with the infrastructure class and count or unavailable-rerun cause. This rule applies to both the
pre-review and pre-merge gates. A setup-step name, a card comment, or a manual flag never
classifies the failure; service evidence must be in that failed step, so a pytest assertion
mentioning a registry or a 503 remains substantive, as does a broken workflow setup. If the code
deliberately does not change for a substantive rejection, for instance when the defect is in a
test or in the gate itself, the worker reports
`--kind blocked` with the analysis instead of another `done`.

A recovered stale worker result bearing that infrastructure class is accepted once into the same bounded gate
path. A further report of that unchanged SHA is Blocked visibly, naming the class and the prior retry, rather
than replaying the first report's request id as a quiet tick.

That gate class and the bring-up classes [below](#bring-up-outcomes) are two axes and not one
vocabulary used twice. The gate sorts a red result over code that exists and calls its other class
`substantive`; a bring-up is sorted before any line of the card's work has been read, and the
sprint's word for its other class is `task`. The two names are one correspondence and nothing more,
so a reader of either side can find the other. What they share is the principle: a failure of the
host is not a verdict about the card.

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

### What a finished phase cost

Every completed worker phase and every completed review phase leaves one `attempt.usage` event on the
card, so phase cost is answerable from the Secretary journal alone and nobody has to reopen a provider
session file to get it. It is a typed board protocol event (`kind: "attempt.usage"`, a Card subject) with
no backend mutation, written through the same append-only audit as every other event: the normal
board/audit export therefore carries it, and a reader selects it by kind rather than by parsing marker
prose. There is no backfill. Cards that finished before this event existed have no usage record, and that
absence is not a zero.

**When it is written.** On the acceptance path itself, and only for an accepted terminal outcome: a
`report:done` or `report:blocked` the dispatcher accepts, and a `review:green` or `review:red` verdict it
acts on. A done report bounced at an already-rejected checkout is not an acceptance and writes nothing.
The write sits where the exact completed run is still on the record with its bound provider session — a
retained worker before its freeze, a reviewer after its pane is confirmed closed but while its run and
session are still recorded — so relaunch, rework and a later recovery cannot re-attribute the account to a
different session.

**What it binds.** The event is self-contained: card ref and subject, the numeric attempt and the attempt
id, the report generation the phase closed, the role (`worker`/`reviewer`) and phase (`worker`/`review`),
the head id, adapter, resolved model and `model_source`, the launch id, the provider `session_id` or its
typed absence with the reason for it, the collection outcome, and the three token accounts below. The
identity fields are the routing journal's own launch snapshot, resolved the same way a routing event
resolves them; this is not a second journal and does not re-read `heads.toml` for a head launched hours ago.

**Token fields.** Five canonical dimensions — `input` (uncached input), `cache_input` (cache
creation/write input), `cache_read_input` (input served from cache), `output` (total generated output,
including reasoning), and `reasoning` (the reasoning subset of `output`) — each a non-negative integer or
`null`. The additive token total is `input + cache_input + cache_read_input + output`; consumers must not
add `reasoning` again. `null` means the provider did not report that dimension, and is deliberately not
`0`: a zero is a real count. The occurrence carries them three
times: `tokens` is the interval this phase owns, `session_totals` is the running provider-session total the phase ended at, and
`phase_baseline` is the boundary it started from, so `tokens = session_totals - phase_baseline` dimension by
dimension and any reader can check it. A `collected` outcome reports at least one dimension in each of the
three; every degraded outcome reports none anywhere, so no reader can mistake an unreadable phase for a
free one. There is no price table and no monetary conversion here.

**Canonical occurrence projection and phase accounting.** One repository projection is the source of
truth for usage. It reads the card's committed and staged TaskAudit records under the audit lock, validates
every `attempt.usage` record through the typed event schema, and returns each immutable occurrence with a
separate `pending` publication flag. An exact committed-plus-pending copy is one occurrence. A request id
with another payload, an event id with another request owner, an unreadable record, or conflicting phase
ownership makes the projection unavailable; readers fail closed instead of silently dropping evidence.
The flag changes only export visibility. It never changes the event's identity, payload or semantic
authority: a successful stage is immediately an accounted occurrence even before its journal append.

Both providers journal a *session*, not a phase, and one session can serve several phases: the ordinary
red-review rework retains the worker and resumes the same conversation into the next round. One rule covers
both adapters. A phase owns the usage recorded after the previous authoritative terminal boundary for that
same card, role, adapter and provider `session_id`, through its own terminal boundary. The predecessor is
selected by the explicit causal identity in the events — numeric attempt, attempt id ownership, report
generation and phase — never by committed or pending iteration order and never by append time. Delayed or
reversed publication therefore cannot change an interval already staged. A session with no matching prior
occurrence starts at zero. When the current provider read produces totals that need subtraction, a matching
predecessor whose typed degraded outcome carries no `session_totals` is an audit failure: the later phase
stays in place and no zero baseline is invented. An unreadable or conflicting projection is likewise an
audit failure. This does not change provider-read failure precedence for the phase being recorded. If its
own provider read is degraded, it needs no interval arithmetic, writes that named outcome and proceeds
normally without rereading the provider. If a readable session
total comes back *below* an authoritative boundary, the evidence contradicts the immutable earlier
occurrence. The later phase writes the typed degraded `arithmetic_contradiction` outcome with no totals and
continues through its lifecycle; it never disguises the contradiction as a collected all-zero interval.

**Adapter aggregation.** Codex writes cumulative `token_count` snapshots, but a new user turn can reset
`total_token_usage` while retaining the same session and rollout file. A decrease in any raw counter starts
a new segment. The session total is the sum of each segment's final snapshot, so it is monotone across one
or many resets; repeated or replayed snapshots within a segment still add nothing. Codex's
`cached_input_tokens` and `cache_write_input_tokens` are contained in `input_tokens`, while
`reasoning_output_tokens` is contained in `output_tokens`. At each segment endpoint the adapter exports
`input_tokens - cached_input_tokens - cache_write_input_tokens` as `input`, retains `output_tokens` as the
inclusive `output`, and exposes `reasoning_output_tokens` as its contained `reasoning` subset. All five raw
fields and valid containment are required for a usable Codex snapshot, so
missing or impossible relations degrade as malformed instead of creating an ambiguous immutable event.
Claude writes one `usage` object per assistant message, and its `input_tokens` is already exclusive of
`cache_creation_input_tokens` and `cache_read_input_tokens`. Those three values map directly to `input`,
`cache_input`, and `cache_read_input`. The session total is their **sum** over distinct messages: a message id
contributes its last usage object exactly once, which is what keeps a streamed message and a resumed
session's repeated records from being counted twice. Claude's `output_tokens` maps directly to the same
inclusive `output` meaning as Codex. After message-id deduplication, the adapter reads
`output_tokens_details.thinking_tokens` as the contained `reasoning` subset. It sums reasoning only when
every contributing usage object supplies a valid detail, including an explicit zero; if any omits the
detail, aggregate `reasoning` is `null` while the known total `output` remains available. A malformed detail
or thinking count greater than that message's output is a malformed usage record.

**Degraded outcomes.** One value says the counts are real and the rest name a specific failure:
`collected`; `arithmetic_contradiction` (a readable current total is below an immutable earlier boundary);
`adapter_unsupported` (the head ran on an adapter with no structured usage records);
`session_unavailable` (no provider session identity was bound to the run); `source_unavailable` (the
structured record source was never bound, or names no journal); `source_unreadable` (the journal exists and
could not be read); `records_malformed`; `usage_absent` (the journal parsed and holds no usage record).
`records_malformed` and `usage_absent` are deliberately different answers: a record that *declares* itself a
usage record — a Codex `token_count`, an assistant message with a `usage` field — and then carries a schema
this adapter does not publish is malformed, and so is a journal in which nothing parsed at all. A journal
that simply holds no usage record is absent. A provider schema change is therefore visible as a broken read
rather than as a phase that cost nothing. A truncated tail — the normal shape of a journal read while its
writer is still around — is one skipped line, not a failed read: the complete records before it are still
counted. `skipped_records` counts everything unusable, both unparseable lines and schema-invalid declared
usage records; `records` counts what the aggregation used.

**Idempotency.** One occurrence per completed phase, keyed by attempt id, phase and the round it closed
(attempt number and report generation). A replayed request or a re-entered acceptance commits the event
that already owns that id rather than a freshly computed one, so a recovery reading a session that has
since grown can neither add a second occurrence nor overwrite the first, and a second phase on a retained
session reproduces its original interval instead of re-reading a larger whole-session total. A repeated done
report inside one round — the infrastructure-classified gate retry — returns the occurrence that round
already owns.

**Durability order.** A completed phase never advances past its own account. On every worker-report and
reviewer-verdict path the occurrence is made durable in the append-only audit *before* the control event and
the transition: it is staged under its request id and then appended. A card may advance past a staged
obligation, because the exact staged record is still owed and a later tick publishes it; it may not advance
past nothing at all.

**Where a staged obligation is settled.** One site, and it is not the card's own tick. A phase can finish
and take its card out of the pipeline in the same breath — `report:blocked` moves it to Blocked, a green
verdict with no observer moves it to Done — and the dispatcher drops that card's record on the way out.
Nothing that walks active cards, dispatcher records or the board would ever reach it again. So every
production tick *begins*, once the singleton, pause and mutation guards have permitted work and before
observer fencing, `ACTIVE_STATES` selection, active-card reconciliation, any phase-boundary read and any new
claim, by publishing every occurrence the canonical projection marks pending. That pass takes no dispatcher
record, no board lookup and no card state as input, and pending usage publication
takes precedence over every piece of card lifecycle work in the tick. A publication failure publishes
nothing in the record's place: the exact staged occurrence stays pending, the tick reports it as a degraded
`attempt-usage-recovery` action naming the cards still owed, and it is eligible again on every later
permitted tick whatever state its card has reached by then. Phase-boundary selection consumes the same
projection, so it sees a staged predecessor directly and does not depend on recovery winning a publication
race first. Publication always finishes the exact staged occurrence, never a re-derived one, so a session file
that grew in between cannot change what the phase was accounted for, and it is idempotent: a record already
appended is simply not in the pending set, and a tick that owes nothing does nothing and reports nothing.

**Non-blocking, and what is not.** Reading the provider never decides anything: a missing session, an
unreadable journal and malformed records are named degraded outcomes inside the occurrence, and the worker
report or reviewer verdict is accepted, the cleanup and freeze unchanged, the card moving exactly where it
was going, and later dispatcher recovery unaffected. Failing to make the occurrence durable is not a
collection outcome — it is an audit failure, and it is raised rather than swallowed. That ends the card's
tick with the phase unadvanced and the report or verdict still standing on the board; the dispatcher's
per-card error handling records it and the next tick retries the same acceptance, which is what keeps the
control event and the transition from outrunning the account of the phase they close.

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

### Revision-bound worker feedback

The description rendered in a worker `TASK.md` is authoritative. A create or description edit has
an immutable audit event and description digest; its event id is the card's current specification
revision. Reviewer verdicts and observer decisions record that revision and digest when they are
written. The task-document feedback selector renders a red review, or a rework decision and any
supporting red review that is present, only when each rendered item is bound to the current revision.
A correctly bound rework decision remains the instruction that opened the round even when no red
review exists or the available red review predates the revision boundary. A reslice followed by
a description edit therefore starts a fresh worker on the edited description without a prior
reviewer's instructions. The selector is deliberately fail-closed: missing, malformed, or ambiguous
audit/comment binding omits historical feedback rather than presenting a possibly superseded order.

### Head heartbeat identity

Every dispatcher-launched worker, reviewer and observer writes one atomically replaced, version-1 JSON heartbeat
before its shell `exec`s the provider. It contains the pid, Linux boot id and process start ticks together with the
durable `HeadRun` id, role, card or sprint binding and the pane leaf once that leaf is known. The writer begins with
the run, role and task binding. Terminal creation and the writer have no ordering guarantee, so pane creation first
atomically writes a matching leaf handoff beside the heartbeat: a later writer incorporates it in its first record,
while an already-written matching record receives a guarded second atomic replace. The writer checks the handoff
again after its base replace, covering both orders without letting a late binder annotate another process that has
reused the pid-file path.

Readers classify a matching live record, a dead record, a live identity mismatch, a missing not-yet-written record,
and an unreadable record separately. Boot, start ticks, run id, role, task and a known leaf all have to agree before
a process counts as this head. Lifecycle and recovery consumers use one HeadRun classification boundary: it constructs
that expected identity before a stop can persist `finishing` or `stopped_by`, a review launch can become `reviewing`,
or a pane/workspace can be relocated or closed. A mismatch is an operator-facing degraded state, not evidence of a
head to adopt or a process to signal: retention, launch recovery, watchdogs, stop paths and observer reconciliation
leave the prior run un-attributed and never open a replacement beside it. The same guard is rechecked immediately
before every destructive pane close, workspace stop and heartbeat signal. Raw command overrides write no heartbeat
and receive no synthetic identity;
they retain only the documented launch grace and pane-output fallbacks.

Whether a head is alive and whether its runtime pane is drawn are two questions with two sources,
and only the first is a statement about the head. A head is alive when its own observation says so:
the heartbeat above, read as a live matching identity whose process is running or suspended, or a
provider cursor bound to that same `HeadRun` that advanced, which proves work is being done. Only
the heartbeat may say a head is gone, because it is the only source that observes the process.
Anything else is `unproven` — a statement about the observation, not about the head — and a role the
dispatcher holds a head identity for but no durable `HeadRun` is unproven by construction, since a
snapshot bound to nothing would have to borrow another run's evidence. Pane and terminal readings
never enter that answer: a pty the renderer draws nowhere, a disconnected pty, a pty no inventory
names and a pane channel that refused are four different facts about the window, and none of them is
evidence that a head is absent. `secretary head-status` is where an operator reads the two halves
apart; see [Head status in a live workspace](OPERATIONS.md#head-status-in-a-live-workspace).

### Worker retention through validation and review

After a worker reports `done`, the dispatcher suspends its live, addressable worker session before
moving the card to Validate. A head with no pane handle is not retained: nothing can address it,
so it is stopped with a confirmed stop instead. The record carries the retained state through the mechanical
gate and through the review that follows it, so the worker cannot change the checkout while either
is judging it. Before the reviewer starts, the dispatcher confirms that suspension from the head's
heartbeat; a session it cannot confirm gets a confirmed stop, and the round loses its continuation.
Nothing here stops every terminal in the worktree, so the worker's own pane stays the reviewer's
split anchor.

Substantive red verdicts return the card to In progress through one transition, and both hand the
round back to the session that wrote the code. An enumerated infrastructure red from the mechanical
gate instead holds the card in Validate while the gate reruns the failed Actions run for the same
SHA, with no `gate-red` transition and therefore no `red_ci` budget event. The rerun has a bounded
per-SHA ceiling and is never emulated by polling the old terminal run; an unavailable rerun or an
exhausted ceiling is Blocked visibly. The same pending-stall ceiling covers a rerun that never
completes on the first gate, the pre-merge re-check, or the release re-check after an observer
decision.
What differs is what opens a substantive rework. A substantive red mechanical gate opens it
directly: nothing about that failed gate is a judgement anyone has to make. A red review on a card
whose sprint
declares a concrete observer opens nothing by itself; the card parks in
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
the delivery proof. A refused `tui-idle` wait carrying `timeout` or `satisfied:false` is explicit
busy evidence, not a transport failure: the readiness wait runs before SIGCONT, so the retained
HeadRun, pane binding, workspace and pending continuation remain exactly as recorded. Its durable
bounded retry delay is not a delivery acknowledgement and never authorizes a stop, replacement or
new lifecycle attribution. Unavailable transport, malformed evidence and `terminal_handle_stale`
remain separately typed conservative failures; absent fields on historical evidence are unknown,
never busy. Recovery after a crash cannot mistake the previous `done` report for a new
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

#### Retained-continuation provider liveness

`worker_continuation_liveness` is version 1 state bound to the exact retained `HeadRun`: its run id
and a digest of immutable launch facts, first busy observation, last provider observation and last
fresh provider progress, opaque provider cursor/source fingerprint, persisted source baseline, busy
count, recovery rung and terminal outcome. It contains no prompt, composer or provider text. A new
record is created only when that retained delivery boundary is written. Missing, malformed,
unsupported or HeadRun-mismatched values are durably `unknown`; the sole unbound serialised shape
is explicit `unknown`. Historical busy counts remain audit data and cannot bind a later run, reset
the ladder or spend a recovery rung.

Before every retained-continuation retry, one central admission step validates the durable episode
and exact `HeadRun`, resolves its launch-bound provider source, and persists/uses the v1 baseline
for that same source. Codex reads only the bound session journal selected from its pre-pane baseline;
Claude reads only the exactly-one transcript selected from its pre-pane baseline. Neither path uses
a workspace-wide newest-file mtime. A later changed opaque cursor is fresh provider progress. It
preserves the same run, workspace, claim, continuation intent and retry owner, resets only the
no-progress ladder, and makes a `tui-idle` busy result non-destructive. Source absence, ambiguity,
a foreign source, malformed source or historical episode without a baseline are typed unavailable or
unknown. Fan-out telemetry and recorder failures never enter that liveness decision. A foreign or
incomplete retained-liveness source cannot become progress, reset or advance the ladder, or authorise
recovery or replacement. The worker/reviewer watchdog carries the same typed source result:
provider-unavailable, stale handle, identity mismatch, confirmed dead and busy are not aliases, and
only admitted observed progress renews liveness.

Once an exact episode rejects a foreign or changing source, it is sealed as `unknown`: the original
HeadRun binding, source baseline, cursor and no-progress ladder remain audit-only and cannot be
re-admitted by a later reply. The shared worker/reviewer status seam independently checks every
apparently accepted provider observation against the persisted HeadRun before it can renew the
watchdog clock.

Codex preflight writes its immutable run descriptor: run id, HeadRun fingerprint, resolved
workspace, role and task reference. Binding selects exactly one journal and retains that descriptor
verbatim, adding only verified journal identity, range, cursor and bind time. The ingress and the
shared worker/reviewer provider reader both validate the same descriptor before admitting a cursor.
A missing, overwritten or foreign field is unavailable or identity mismatch, never evidence for a
different retained run.

An unchanged cursor records either `completed_turn_residual_composer`, when equal non-empty composer
and output fingerprints prove the old composer is residual, or `active_or_unknown_turn`; screen text
is never read as the distinction. Only after three unchanged admitted busy observations does the
dispatcher persist a single `safe_recovery_pending` rung before asking for an explicit
provider/terminal-safe capability. There is no raw interrupt, generic key chord or screen-derived
recovery action. The current host has no such capability and records its typed absence, then takes
the existing confirmed-stop/HeadRun fence to one replacement. A future capability must return a safe
receipt bound to that same run; its recorded response window is rechecked for admitted provider
progress and can return to normal delivery exactly once. No admitted progress after that window or
an unavailable/refused capability ends in the recorded replacement path. A source identity failure
remains a typed blocked outcome, not a reason to touch a potentially foreign pane. A stop that cannot
yet be confirmed remains identity-fenced, never opens a second worker.

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
             "resource": "openai-sub", "account": "openai-subscription",
             "session_id": "0198b0b0-...", "session_id_reason": "",
             "prompt_path": "/workspaces/PROJECT-N/TASK.md",
             "prompt_version": "sha256:..."}]}}
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

Each launch record also carries the provider's durable `session_id`: the id in the Codex rollout or the
Claude jsonl session. If an adapter cannot expose it at bring-up, `session_id` is explicitly `null` and
`session_id_reason` says why. `prompt_path` names the task document delivered to that role, and
`prompt_version` is its `sha256:` content address at that same bring-up. These facts let a journal reader
follow a round directly to its conversation and prompt without reconstructing either from a workspace and
time window.

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

### Bring-up outcomes

A bring-up is everything between a card being given to a head and that head existing. When one
produces no head, the outcome is classified in a single place for both paths — the worker's (claim,
respawn, rework) and the reviewer's (`start_review`) — so what a blocked card says never depends on
which caller wrote it. There are two classes, and a closed set of causes decides which one an
outcome has:

- `infrastructure` — `pane_never_ready`, the head's pane was busy or held in a dialog for every
  attempt this role was given and never took its launch prompt; `launch_aborted`, a launch that may
  have left a head running and is therefore not turned into a second one; `host_unavailable`,
  everything else the host could not do, from a pane that would not open or an inventory that would
  not answer to a registry that cannot supply a usable broad-check contract;
- `task` — `workspace_contract`, a failure of this card's own bring-up contract: the checkout it was
  requeued onto is gone, or is not the worktree on the branch its claim recorded.

The cause decides the class, so no raise site and no caller may pair them freely, and a cause
nobody recognises is ignored rather than trusted. The rule behind the split is what the failure says
about the card, and a bring-up says almost nothing, because at that point no line of the card's work
has been read, let alone judged. So a failure of the host leaves the card carrying no verdict, and
the one family that is a verdict is the card's own contract, which no healthy host survives and no
later tick repairs.

The class is durable in the action token of the transition the block writes. An infrastructure
outcome puts `-infrastructure-` in front of `-blocked` — `bringup-infrastructure-blocked`,
`worker-respawn-infrastructure-blocked`, `rework-infrastructure-blocked`,
`review-infrastructure-blocked`, `contract-preflight-infrastructure-blocked` — and a task outcome
keeps byte-for-byte the action token it always had. Reading the class is reading that token back: a
request id containing `-infrastructure-blocked` is an infrastructure outcome and every other block
is not. That is a reading which survives the tick, the pane and the log, and it is the only one:
nothing infers the class from a message, a role or a caller. Deliberately it is not card metadata
either, for the same reason the block classification of a report is not — a second write that can
fail on its own would leave a card field silently disagreeing with the audit.

The card's Blocked reason and the tick's outcome are built from one object, so they say the same
thing in the same words. The reason ends in a clause naming the class, the cause, the stage
(`claim`, `respawn`, `rework`, `review`), which head it was and the attempt id, followed by the
sentence the class entails: for an infrastructure outcome, that the head never came up, so this is
not a verdict about the card. The tick's outcome carries `failure_class`, `failure_cause`, the same
`failure_reason` string the card was given, and a `bring_up` object with the same fields plus the
host's own detail and, where the pane was the cause, its readiness and how many attempts it had.

Who decides what happens next is split on purpose. The dispatcher classifies the outcome and
presents the evidence — what did not come up, at which step, under which `attempt_id` — and stops
there. After an infrastructure outcome it opens no new attempt, schedules no return and moves the
card nowhere else. Whether that card is tried again or the sprint is blocked is the observer's
decision, taken on that evidence and carried out the ordinary way, by moving the card out of
Blocked; a card that comes back to Ready is claimed again under a fresh attempt id.

The bounded retries that exist are all spent before an outcome is written, and none of them ever
turns into a verdict about the card. A pane that is busy or held in a dialog parks the bring-up
instead of failing it: `worker-launch-deferred` or `review-launch-deferred`, one attempt per tick up
to the configured ceiling, each deferral naming which attempt it is, the counter on the record so a
deferral nobody wrote down cannot become an unbounded retry, and the counter reset when that role's
head does come up. A probe nobody answered is deliberately not deferred. When the ceiling is spent,
the outcome is `pane_never_ready` and therefore infrastructure: exhausting the ceiling ends the
waiting, it does not convert into a judgement of the card. The reviewer's bounded relaunch over a
green candidate, in [Candidate history](#candidate-history), ends the same way.

An infrastructure outcome charges the sprint nothing. It is still recorded, because an observer has
to see how often the host failed to bring a card up, and it is recorded apart: as the uncharged
event type `infrastructure_blocked`, visible as `budget.uncharged` in `sprint show` and `sprint
status`, entering neither `total` nor the `signal` and `hard` thresholds. Every other block — a
worker's own report, the gate, a merge, a release — charges `blocked` exactly as it always did. Both
families go through the same budget write and are told apart only by the token above. A sprint
stored before the field existed reads as zero; there is no migration.

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
the rewritten binding by a repeat `project add`, as are `orca_binding` and curator-only `curator_roots`. The scanner and provisioning prepare
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
id derives from it, so provision results and dispatcher-owned exact-SHA gate receipts from an earlier cycle cannot be reused on an
unchanged scanner head. Evidence is bound to the cycle that produced it.

Diagnosing failures, recovering a stale disabled draft, re-onboarding an enabled legacy project and
verifying a passed result are described in
[Operations](OPERATIONS.md#connecting-a-project-gate-and-stale-input-recovery).

## Memory

Facts are stored flat as `memory/facts/global/<slug>.md` or `memory/facts/<project-dir>/<slug>.md`. One
fact is one distilled markdown record. The curator is the writer role; every other agent reads through
`memory_search`, `memory_get` and `memory_list`.

Curator input is a bounded, two-phase batch protocol. Before selection, each source receives a route from the
selected instance's project registry: a normalized descendant of one registered `repo` is its canonical `id`; a
safe optional `orca_binding` additionally supplies that binding's Orca workspace tree
(`<workspaces root>/<orca_binding>/...`). That durable boundary still routes a recorded historical cwd after the
per-card worktree leaf has been removed. A binding may additionally declare absolute `curator_roots` for named
ad-hoc historical checkout trees; these roots affect curator input only and grant no execution authority. Exact
directory boundaries and an unambiguous route are required. Empty, relative, unreadable, malformed, unregistered or ambiguous paths are
`unknown`; installation-wide sources are `global`. An observer workspace named
`workspaces/observers/sprint-<token>` restores the `sprint:` prefix removed by the dispatcher's token before it
consults the board. It has no inferred owner: one registered structured reservation routes to that project; multiple
distinct registered reservations route to the reserved `review:po` selector, while malformed, duplicate or
unregistered sets remain `unknown`. Harvest, precheck and advance enter the same cursor-settlement
transaction: a curator-local advisory flock serializes watermark.json and pending.json without owning the dispatcher
lifecycle. `harvest --project <canonical-id|review:po>` filters routes before taking the deterministic bounded prefix; omitted
`--project` explicitly means all backlog. A pending batch records and signs this selector, so a retry or advance
with a different selected project or all-backlog mode fails closed. `curator backlog [--project <canonical-id|review:po>]
[--json]` only reports deterministic aggregate route/head metadata (session, signal-turn and memory-file counts plus
timestamp bounds); selected JSON with no pending batch and baseline-valid cursor state also carries one opaque cutoff
identity and cursor count. It
creates no pending record and changes no cursor. A selected batch with turns or memory is written
as a versioned pending record bound to the current curator workspace/run/session identity and selector; a retry replays it exactly. A fresh scan
that classified only complete non-emitting records advances those precise cursors atomically instead, and
never writes pending.json. Advance accepts only the fact-bearing pending form, verifies its identity and each
source's starting cursor, then moves only the listed cursors. Legacy line-based watermarks remain readable
until a selected source advances to a byte cursor; unversioned, stale, foreign, corrupt, or cursor-only pending
data is not guessed, rewritten or advanced. A row-limited scan records complete non-emitting or oversized
records it has classified, so noise prefixes cannot stall a source. An incomplete trailing JSONL record is
source-local: its cursor remains at its last complete record, while safe records from that source and later
sources still settle. The partial source is reported for observability and is retried from that cursor after its
writer completes the row. Precheck takes
the transaction nonblocking; contention returns the dedicated 102 defer result, which the gate answers
successfully without dispatch or cleanup. flock ownership is released by the OS if its holder exits.

### Project baseline settlement

`python3 -P -m triggered_agents curator baseline` is the narrow operator path for intentionally settling existing
curator input without running the curator, changing its schedule, or writing memory facts. It accepts one registered
canonical project id or the reserved `review:po` selector, an explicit actor, a non-empty one-line reason, and exactly
one opaque evidence identity:

```bash
python3 -P -m triggered_agents curator backlog --project PROJECT --json
python3 -P -m triggered_agents curator baseline \
  --project PROJECT --actor OPERATOR --reason 'reviewed historical backlog' --cutoff-id CUTOFF_ID

# The same audited flow settles manually reviewed multi-project observer input.
python3 -P -m triggered_agents curator backlog --project review:po --json
python3 -P -m triggered_agents curator baseline \
  --project review:po --actor OPERATOR --reason 'reviewed multi-project observer backlog' --cutoff-id CUTOFF_ID

# Or settle the exact fact-bearing pending batch already returned by `harvest --json`.
python3 -P -m triggered_agents curator baseline \
  --project PROJECT --actor OPERATOR --reason 'approved pending batch' --batch-id BATCH_ID
```

For a selected project with no pending batch and baseline-valid state, JSON backlog output includes `cutoff.id` and
`cutoff.cursor_count`.
The cutoff id binds the project, each selected source's starting watermark and its current complete terminal cursor;
it is metadata only, never a source path, cursor value, transcript, or personal-memory body. A changed source,
incomplete JSONL tail, malformed watermark, stale proof, or empty cutoff is refused. A fact-bearing pending batch is
instead bound by its existing versioned identity, selector and starting cursors; its `batch_id` is the alternative
evidence identity. A baseline never accepts the all-backlog selector, cannot bypass any pending record, and rejects a
foreign, ambiguous, mismatched, malformed, or stale source before state changes.

The callable API is `triggered_agents.agents.curator.cli.baseline_settlement`, with the same required `project`,
`actor`, `reason` and exactly one of `cutoff_id` or `batch_id`. It runs under the same local cursor-settlement lock as
harvest and advance. The transition writes the watermark, removes a selected pending record when settling that exact
batch, and publishes `baseline-audit.ndjson` in the curator state directory together with rollback on a local write
failure. Each
audit event records version, time, project, actor, redacted reason, evidence kind/id, outcome, and hashed affected
cursor identities/count; it contains no transcript text, personal-memory text, fact content, raw source payloads or
credentials. Command output names only the selected project and cursor count.

### Memory read identity

The Memory MCP endpoint requires FastMCP Bearer authentication. Before a head is opened, its launcher
writes a digest-only access grant bound to that exact `HeadRun` and puts the opaque bearer capability in
the launched process, not in `runtime.env`. The service resolves the grant itself and rechecks the
HeadRun heartbeat on every read. Missing, expired, malformed, foreign or stopped bindings return a typed
data-free denial. `caller`, `scope`, and a tool's other arguments are never authority.

The resolved policy is deliberately small: an interactive PO has installation-wide read; every worker or
reviewer has exactly its card's `project:<id>` plus `product:secretary`, including when the project id is
`secretary`; an observer has its sprint reservations plus `product:secretary`. The curator and
retro standing duties have installation-wide read because canonical deduplication and retro's canon-hygiene
review compare facts across projects. Steward has only `project:secretary` and `product:secretary` for its
system watch. Other runtime roles have no memory-read grant. A requested scope only narrows that set.
Search does not retry at a wider scope, and `memory_get` and `memory_list` use the same guard as search.

An ordinary Claude or Codex session reaches that interactive identity through the installation-owned
`secretary-memory-po-bridge` stdio MCP server in its user configuration. The bridge creates a PO HeadRun,
keeps the bearer inside the bridge process, and deletes its heartbeat and grant on exit. Dispatcher-launched
Claude heads use `--strict-mcp-config`; Codex heads disable `po_memory` with a command-line override. Both
receive only the direct HTTP `memory` server and their launch-bound bearer, so project authority is selected
by the server-side grant rather than by a client config or a model-supplied argument.

The memory search log is an authorization audit, not a fact transcript: every record identifies its action
and records the resolved role, subject, scopes and outcome with result ids/scores, never fact text, queries
or bearer material. Consumers that need evidence of a search filter for `action == "memory_search"`.

### Restart health contract

Memory restart requested by an upgrade, unit change, product-code/dependency change, or shipped-pack
reconciliation is not healthy merely because systemd says the service is active. Upgrade creates a
short-lived steward `HeadRun` and its ordinary launch-bound grant, hands the complete temporary bindings
tree to the configured runtime account, then uses a narrowly dropped-privilege child of that account to
publish the versioned heartbeat and perform `initialize` then `tools/call(memory_list)` over the MCP
endpoint. The service therefore verifies the bearer and the same live heartbeat/read guard used by heads;
it never has to inspect a root-owned upgrade process. The probe supplies neither `caller` nor `scope`,
expects a Secretary/product-scoped row, and removes its temporary grant and heartbeat afterwards. An
unavailable service, stale or denied identity, malformed MCP reply, or absent expected row fails the
`memory` upgrade step visibly. A returned denial is typed and final, rather than being retried into a
missing-row timeout. The pinned list tool may serialize one JSON text block per row or use structured
content; the probe normalizes every supported form to rows, so an ordinary single allowed row is not
mistaken for a denial.

The portable acceptance fixture in `tests/test_memory_acceptance.py` makes only temporary sentinel rows. It
uses the production token verifier and read tools to prove the interactive PO, foreign worker, Secretary
worker, and explicitly reserved observer matrix, including that spoofed `caller`, wider `scope`,
`memory_get`, and `memory_list` cannot widen it. It inspects only the audit schema and does not put fact
text or bearer material into the audit assertion. `tests/test_memory_health.py` also starts a local Memory
MCP daemon with the pinned streamable-HTTP protocol and drives the same initialize/session/list exchange;
its root-only portability regression additionally verifies the root-parent/runtime-user/daemon-reader
ownership crossing without touching an installation. CI installs the declared `memory` extra, so the
streamable-HTTP daemon tests exercise initialize/session handling and both allowed and denied list rows;
the wire tests cover every supported result form, and the privileged cross-user regression remains an
explicit root-only check.

Bearer delivery assumes processes running as the same host user are mutually trusted: that user can inspect
another same-user process's environment or command line and could reuse its live bearer. The protocol limits
cross-user and stale-head access, keeps the capability out of durable output and redacts it from launch and
audit output; it is not a generic same-user credential isolation scheme.

Write authority is split in two, because asking for a fact and publishing one are different acts.
`propose` stages a fact in the curator inbox (`<data_dir>/memory/.staging/<propose-id>`) and touches no
canon; `commit` and `supersede` write `state/memory` in the instance repository. The **proposer** roles
are `curator`, `secretary`, `operator` and `butler`; the **canonical writer** roles are `curator`,
`secretary` and `operator`. A butler may therefore hand the curator a fact it noticed, but its own
`commit` or `supersede` is refused with a permission error saying butler proposals await curator review,
not with the generic write refusal an unknown role gets. Every actor may still only use a `source` of its
own role, so a butler proposal stays visibly butler-sourced through commit. Publication of a proposal
owned by another actor remains the existing ownership rule: an actor commits its own proposal, and a
`secretary` or `operator` actor may commit anyone's.

Facts whose current owner cannot be resolved without product-owner judgment use the dedicated
`review:po` scope (`state/memory/facts/po-review`). This is a triage basket, not operational truth:
entries should carry the `pending-review` tag, identify their source sprint/session, and state the
candidate project scopes. Interactive PO, curator, and retro identities may read it; worker, reviewer,
observer, and steward grants never receive it. The PO can inspect it explicitly with
`scope=review:po`, then publish the resolved fact into `global` or `project:<dir>` with `supersede`,
atomically removing the review entry.

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
