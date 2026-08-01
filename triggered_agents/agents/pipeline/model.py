"""Pipeline domain model — pure, no I/O.

The pipeline board is the operational task queue: the PO (secretary) writes specs into
cards, a deterministic dispatcher claims Ready cards and spawns workers, workers report
via comments. Column transitions are guarded here, in code, keyed by role — not by trusting
a prompt. A worker can never move a card; the dispatcher can never skip a guard.

The one path into "In progress" is the `claim` command, not a `move`. Claim is separate
because entering work is not a plain transition: it also picks a worker, checks the
predecessor is Done, enforces one code task per project, and honours a global cap. Folding
all that into the move matrix would hide it. So Ready->"In progress" is deliberately absent
from TRANSITIONS, and `move` to In progress errors with a pointer at `claim`.

steward has every po transition plus one override, Blocked->Done (STEWARD_OVERRIDE): a legal
replacement for a human editing the board's raw Kanboard API to force a card past review, which is
what happened on agent-kanban-232/235 and triggered-agents-230. The override still needs a paper
trail, so ops.move_card requires a non-empty justification comment in the same call before it will
run it; every other role stays exactly as guarded as before.

steward can also move any active card straight to Blocked (2026-07-04 design grill, memory:
design decision) — the one escalation path its whole mandate rests on ("if you cannot resolve it
yourself, move it to Blocked with an analysis and wait for a human", no numeric caps). This is a manual
break-glass action a human would otherwise have to do by hand through the Kanboard UI when the
dispatcher itself is the thing that broke (a claimed In-progress/Validate card's watchdog never
fires if the tick that would run it is dead). The dispatcher's own In progress/Validate -> Blocked
stays automatic and unaffected; this is steward reaching in from outside that loop. ops.move_card
requires the steward's escalation reason in the same call, so a Blocked card records why a human is
needed.

steward also gets In progress -> Done (STEWARD_REPORT_DONE, triggered-agents-255), but only for its
own wake-up report card: ops.create_report_card puts that card straight into In progress (never
through Ready/claim, already self-claimed in the same call) so the dispatch machinery can hand a
head a card to work before it even starts, and this is the one legal way back out of it once the
report is written. ops.move_card gates it further — the card must actually carry
META_STEWARD_REPORT, so this can never become a side door for closing an ordinary code/research
card out of review.
"""
from __future__ import annotations

import os

# Env override exists so the e2e can drive a throwaway board without touching the real one.
BOARD_NAME = os.environ.get("TA_PIPELINE_BOARD", "Pipeline")

COLUMNS = ["Issues", "Ready", "In progress", "Validate", "Assessment", "Blocked", "Done"]
# Where an agent proposal lands: the board's first column. The card is stamped record_type=task,
# so it reads as an execution card awaiting PO triage and never as a Product issue: an agent
# still cannot choose product, kind and priority, so `issue create` stays closed to it.
PROPOSAL_COLUMN = COLUMNS[0]

ROLES = ("po", "dispatcher", "worker", "reviewer", "steward", "retro")

# The board's decision column: a card waits there for a person, not for a machine. `secretary`
# owns the Pipeline board schema and the role/transition model; this surface is a consumer of that
# board, not a second authority over it, so the po and observer releases out of Assessment are not
# restated here. They go through `python3 -m secretary task move`, which is the authoritative
# writer. Two role tables for one board is what produced the defect this rule exists to prevent.
# The one edge kept here is the steward escalation below, because this is the surface the steward
# actually moves cards with.
ASSESSMENT = "Assessment"

# Two types along one axis: does the card change code. A code card produces a PR (and takes part in
# the per-project claim gate); a research card only produces a report. A third "debug" type was
# removed: it produced a PR like a code card but did not count towards the gate, so two PR cards of
# one project could run in parallel.
TASK_TYPES = ("code", "research")

# Metadata keys (flat str->str dict on the Kanboard card):
#   task_type    one of TASK_TYPES
#   project      source project name (also the swimlane)
#   blocked_by   reference of a predecessor card that must be Done before claim
#   head         worker head profile id (heads.toml [profiles.*]); empty -> heads.DEFAULT_PROFILE.
#                On a watchdog retry-switch this is overwritten with the head actually launched,
#                so a later reclaim keeps using it instead of bouncing back to the original.
#   resolved_head worker profile id actually selected at claim time after health fallback. Empty
#                means "not currently claimed", so board UI/list fall back to `head` or default.
#   review_head  reviewer head profile id for Validate layer 3; empty -> heads.reviewer_head();
#                "none" -> PO explicitly disabled layer 3 for this card only.
#   resolved_review_head reviewer profile id actually selected when Validate layer 3 spawns after
#                health fallback. Empty means no active/current reviewer profile.
#   claim        worker/workspace id, set by the claim command
#   slug         short [a-z0-9-]{1,30} tag naming the card's worker/reviewer workspace; a card
#                without one (old/manual) falls back to a transliterated slug of its title
#   base_branch  overrides the project's manifest base_branch for this card only (worker.
#                resolve_base_branch): worktree bring-up and PR/merge target both use it instead
#                of the manifest default. Empty -> manifest base_branch (default main), unchanged
#                from before this field existed. For sprint shim projects (dnd-simulator): cards
#                of one sprint all carry base_branch=sprint/NNN-slug so main stays closed until
#                the sprint's own closing PR.
#   retry_same   watchdog same-head auto-retries already used on this card's current life (int,
#                as a string); reset to "" on every arrival in Ready (move_card)
#   retry_switch watchdog head-switch auto-retries already used (int, as a string); same reset
#   retry_heads  comma-joined heads this card's watchdog has already used (this life), so a
#                retry-switch never re-picks one it already tried; same reset
# retry_* live on the card, not in the dispatcher's local cards.json record: they must survive a
# dispatcher redeploy (which only ever replaces the local state), and a human moving a card
# Blocked->Ready is exactly the "fresh start" the Ready-reset gives them for free.
META_TASK_TYPE = "task_type"
META_PROJECT = "project"
META_BLOCKED_BY = "blocked_by"
META_HEAD = "head"
META_RESOLVED_HEAD = "resolved_head"
META_REVIEW_HEAD = "review_head"
META_RESOLVED_REVIEW_HEAD = "resolved_review_head"
NO_REVIEW_HEAD = "none"
META_CLAIM = "claim"
META_SLUG = "slug"
META_BASE_BRANCH = "base_branch"
META_RETRY_SAME = "retry_same"
META_RETRY_SWITCH = "retry_switch"
META_RETRY_HEADS = "retry_heads"
# "1" on a card ops.create_report_card made (the steward's own wake-up report, triggered-agents-
# 255) — never set any other way. The only thing this flag gates is STEWARD_REPORT_DONE below; it
# is not a general-purpose "belongs to steward" marker.
META_STEWARD_REPORT = "steward_report"
META_RECORD_TYPE = "record_type"
RECORD_ISSUE = "issue"
RECORD_PRODUCT = "product"
# An execution task record: the card the pipeline works on, as opposed to a product or an issue.
# Set on every card this agent creates, so an Issues-column proposal reads as an execution card
# awaiting PO triage and never as a Product issue.
RECORD_TASK = "task"

IN_PROGRESS = "In progress"

# Steward's manual escalation moves. ops.move_card requires a non-empty reason for these, but the
# dispatcher keeps its own automatic In progress/Validate -> Blocked moves reasonless.
STEWARD_ESCALATIONS = {
    ("Issues", "Blocked"),
    ("Ready", "Blocked"),
    ("In progress", "Blocked"),
    ("Validate", "Blocked"),
    # Assessment is a wait on the observer, not on a head, so nothing times it out on its own.
    # The steward's escape hatch has to reach it too, or a card whose observer never came back
    # would sit there with no way out short of a human editing Kanboard by hand.
    ("Assessment", "Blocked"),
}

# Allowed (from, to) column moves per role, for the generic `move` command. Claim owns the
# only entry into In progress and is not listed here (see module docstring).
TRANSITIONS: dict[str, set[tuple[str, str]]] = {
    "po": {("Issues", "Ready"), ("Blocked", "Ready")},
    "dispatcher": {
        ("In progress", "Validate"),
        ("In progress", "Blocked"),
        ("In progress", "Ready"),   # watchdog auto-retry requeue (teardown -> Ready -> reclaim)
        ("Validate", "In progress"),
        ("Validate", "Blocked"),
        ("Validate", "Done"),
    },
    "worker": set(),
    # The layer-3 reviewer never moves cards (the dispatcher acts on its verdict, like it does on
    # a worker report). Its only artifacts are the verdict comment and proposal cards.
    "reviewer": set(),
    # retro (the daily fail-pattern scan) never moves a card either, same reasoning as reviewer:
    # its only board write is a proposal card (ops.retro_idea) in Issues, never Ready or beyond.
    "retro": set(),
    # steward gets every po transition plus one override: Blocked -> Done, a legal replacement for
    # the raw-API Blocked->Done edits seen on agent-kanban-232/235 and triggered-agents-230. That
    # override is only safe with a paper trail, so check_move alone does not gate it — ops.move_card
    # additionally requires a non-empty justification comment in the same call (see STEWARD_OVERRIDE).
    # Plus escalation to Blocked from every active column (its "give up, wait for a human" escape
    # hatch, see module docstring). Done is deliberately absent as a source, there is nothing
    # left to escalate on a finished card.
    "steward": {
        ("Issues", "Ready"), ("Blocked", "Ready"), ("Blocked", "Done"),
        ("In progress", "Done"),
    } | STEWARD_ESCALATIONS,
}

# The one transition in TRANSITIONS that needs more than a role/column check: a non-empty
# justification comment, supplied in the same ops.move_card call. Kept here (not just inline in
# ops) so model stays the single source of truth for "what Blocked->Done even means".
STEWARD_OVERRIDE = ("Blocked", "Done")

# The other transition that needs more than a role/column check: the card must carry
# META_STEWARD_REPORT (ops.move_card checks metadata before allowing it) — see module docstring.
STEWARD_REPORT_DONE = ("In progress", "Done")

# Comment markers, single source of truth for ops (and, later, the dispatcher reading back):
#   [report:done] / [report:blocked]  worker report
#   [feedback]                        worker's feedback on the spec/process, harvested by retro
#   [review:green] / [review:red]     the layer-3 reviewer's verdict
#   [<role>]                          plain comment
MARKER_REPORT_DONE = "report:done"
MARKER_REPORT_BLOCKED = "report:blocked"
MARKER_FEEDBACK = "feedback"
# Dispatcher verdicts on a Validate card's CI (layer 1). ci-green marks "layer 1 passed, running
# the review" — posted once per code state (the dispatcher checks the comments after the card's
# current baseline before posting, so a rework that re-enters Validate gets a fresh note).
MARKER_VALIDATE_GREEN = "validate:ci-green"
MARKER_VALIDATE_RED = "validate:ci-red"
# Dispatcher verdicts on the stand run (Validate layer 2: deploy the PR branch to the project's
# persistent stand and run e2e). Only for projects with a [stand] manifest section. stand-green
# marks "layer 2 passed", posted once per code state; layer 3 (the reviewer) runs after it.
# stand-red is posted per failed run; two consecutive stand failures send the card to Blocked
# (one auto-retry).
MARKER_STAND_GREEN = "validate:stand-green"
MARKER_STAND_RED = "validate:stand-red"
# The layer-3 reviewer's verdict (Validate layer 3: an independent LLM head, not the worker, reads
# the whole repo + PR and posts one verdict). green = all layers clear, the card waits for a human
# merge. red = at least one blocker in any lens; the dispatcher returns the card to In progress
# with a nudge, up to a cap of returns (then Blocked for a human).
MARKER_REVIEW_GREEN = "review:green"
MARKER_REVIEW_RED = "review:red"
# Posted once when a stand-project card's green review triggers the dispatcher's own squash merge
# (all three gates — CI, stand-green, review:green — cleared, on the decision that a live-stand
# end-to-end run is enough assurance to drop the human from the merge for those projects).
MARKER_AUTOMERGE = "validate:automerge"
# The dispatcher's own note when a red verdict sends a card back for rework. Deliberately NOT a
# review:* marker: the invariant "only the reviewer posts a verdict" must not hinge on baseline
# arithmetic — if this carried [review:red] and any baseline shift re-read it, the card would loop.
MARKER_REVIEW_RETURN = "validate:review-return"
# Dispatcher audit note when a PO set review_head=none and Validate skips only layer 3.
MARKER_REVIEW_SKIPPED = "validate:review-skipped"
# Posted once after a successful claim/bring-up, when the worker workspace and head are live.
MARKER_CLAIM_STARTED = "claim:started"
# Posted once when validating a single card blows up unexpectedly (e.g. a base workspace.toml that
# won't parse). The failure is localized to that card — the tick keeps going for the others.
MARKER_VALIDATE_ERROR = "validate:error"
# A watchdog auto-retry requeue (same head or a head switch) on an In-progress card — see
# dispatcher._watchdog_retry. Never posted on the terminal Blocked (budget exhausted): that one
# stays a plain [dispatcher] comment, same as before this marker existed.
MARKER_WATCHDOG_RETRY = "watchdog:retry"
# The steward's justification for a Blocked->Done override (STEWARD_OVERRIDE), posted by
# ops.move_card in the same call as the move itself — never a bare [steward] comment, so the
# reason a card skipped review is always attached to the transition that needed it.
MARKER_STEWARD_OVERRIDE = "steward:blocked-done"


def merge_status(mergeable: str | None, merge_state: str | None) -> str:
    """Normalize GitHub's two mergeability axes into one base-freshness signal, kept apart from the
    CI rollup (triggered-agents-442). `mergeable` (MERGEABLE/CONFLICTING/UNKNOWN) answers "is there
    a textual conflict"; `mergeStateStatus` (CLEAN/BEHIND/DIRTY/BLOCKED/UNSTABLE/UNKNOWN) answers
    "how does the head sit against the base". Only two states drive recovery: CONFLICTING (a real
    text conflict, DIRTY is its mergeStateStatus twin) and BEHIND (base moved ahead, no conflict —
    a clean auto-update). CLEAN and a still-computing UNKNOWN both fall through to the ordinary CI
    path: a transient UNKNOWN (GitHub hasn't finished recomputing mergeability right after a push)
    is never treated as a conflict nor as a green state. Pure domain logic, so it lives here rather
    than in worker.py's host boundary and both poll_pr and merge_recovery read it."""
    mergeable = (mergeable or "").upper()
    merge_state = (merge_state or "").upper()
    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        return "CONFLICTING"
    if not mergeable or mergeable == "UNKNOWN" or merge_state == "UNKNOWN":
        return "UNKNOWN"
    if merge_state == "BEHIND":
        return "BEHIND"
    return "CLEAN"


class GuardError(RuntimeError):
    """A role, transition, or claim guard was violated."""


def check_move(role: str, from_col: str, to_col: str) -> None:
    """Raise GuardError unless `role` may move a card from `from_col` to `to_col`."""
    if role not in ROLES:
        raise GuardError(f"unknown role {role!r} (roles: {', '.join(ROLES)})")
    for col in (from_col, to_col):
        if col not in COLUMNS:
            raise GuardError(f"unknown column {col!r} (columns: {', '.join(COLUMNS)})")
    if (from_col, to_col) in TRANSITIONS[role]:
        return
    # A move in or out of Assessment that is not the steward escalation is a release, rework or
    # reslice decision. That decision has one writer, and it is not this surface.
    if ASSESSMENT in (from_col, to_col):
        raise GuardError(
            f"role {role!r} may not move {from_col!r} -> {to_col!r} here: moves in and out of "
            f"{ASSESSMENT!r} other than the steward escalation to 'Blocked' go through "
            "`python3 -m secretary task move`, which owns the role matrix for that column"
        )
    # Validate->In progress is a legit rework transition (in the matrix); any OTHER move into
    # In progress is the fresh-claim case, which must go through `claim`, not `move`.
    if to_col == IN_PROGRESS:
        raise GuardError(
            "move into 'In progress' is not allowed; use `claim --ref R --worker ID` "
            "(dispatcher only): it also sets the claim and runs the entry guards"
        )
    raise GuardError(f"role {role!r} may not move {from_col!r} -> {to_col!r}")
