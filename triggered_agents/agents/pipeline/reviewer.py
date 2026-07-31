"""Layer-3 reviewer prompt — the one-time REVIEW.md handed to the independent review head.

Validate layer 3 ("LLM review against the spec"): once the cheap mechanical layers are green (CI,
and the stand for stand projects), the dispatcher spawns a fresh head that is NOT the card's worker
and has no write access to the code. It reads the whole repo and the full PR (not just the diff) and
posts one structured verdict, then the dispatcher acts on it like it acts on a worker report —
including, on green, squash-merging the PR itself (validate.py). A green verdict is no longer
double-checked by a human before merge, so this prompt also has the reviewer verify whatever live
checks the worker's report claims. Safe local checks are re-run in the review worktree; heavyweight
checks that need Docker, a stand or external writes are verified through the exact green mechanical
gate for the current head SHA, its workflow and logs. This is the class of proof-of-work a human
skim used to catch without forcing a read-only reviewer to repeat side effects that the lower
validation layers already own.

This module only builds the text of REVIEW.md. The host side (worktree + head) lives in worker.py,
so the dispatcher keeps talking to a single host boundary. The thermo-nuclear quality lens is not
copied into the source: the skill file is read at build time and embedded verbatim into the prompt
(a deliberate decision — use it as it is, read the file into the reviewer prompt rather than
duplicating it in code), falling back to a load-it-yourself instruction if the file is missing.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import card_comments, model, naming, worker

THERMO_SKILL = Path(os.environ.get(
    "TA_THERMO_SKILL",
    str(Path.home() / ".claude/skills/thermo-nuclear-code-quality-review/SKILL.md")))

# Prior rounds of validation on this same card: the reviewer's own verdicts and the dispatcher's
# note when a red one sent the card back for rework. These three are what makes round N aware that
# round N-1 happened at all.
_ROUND_MARKERS = (model.MARKER_REVIEW_GREEN, model.MARKER_REVIEW_RED, model.MARKER_REVIEW_RETURN)

# The history is capped on two axes at once, because it grows on two axes at once: a card collects
# many comments, and single comments (CI log dumps, full worker reports) run to tens of kilobytes.
# A count cap alone still lets one log sheet swallow the prompt; a length cap alone still lets forty
# short comments bury the spec. Caps are per-section since the sections are not equally valuable —
# spec clarifications and prior verdicts are the reason for showing history at all, so they get
# their own budget instead of competing with chatter for the tail of one shared list. Everything is
# a tail: the newest comments are the ones that describe the state under review. `pipeline show`
# stays the escape hatch for anything clipped.
_SPEC_NOTE_LIMIT = 5     # same budget taskdoc gives the worker's operator context
_ROUND_LIMIT = 6         # ~3 red rounds, each a verdict plus the dispatcher's return note
_HISTORY_LIMIT = 10
_COMMENT_CHARS = 2000


def _clip(body: str) -> str:
    if len(body) <= _COMMENT_CHARS:
        return body
    return (body[:_COMMENT_CHARS].rstrip()
            + f"\n\n… clipped, {len(body) - _COMMENT_CHARS} more characters "
              "(full text in `pipeline show`)")


def _parse(comments: list[dict]) -> list[tuple[str, str, str]]:
    """Card comments as (stamp, marker, body), scrubbed and clipped. Scrubbing runs here rather
    than at the section level so no path into the prompt can skip it: a comment body is board text
    that a worker or a CI log may have carried a token into, and REVIEW.md is written to disk in
    the reviewer's workspace."""
    out = []
    for c in comments or []:
        marker, body = card_comments.split_marker(worker.scrub_secrets((c.get("text") or "").strip()))
        if not body:
            continue
        out.append((card_comments.format_ts(c.get("ts")), marker, _clip(body)))
    return out


def _entries(picked: list[tuple[str, str, str]]) -> list[str]:
    lines = []
    for ts, marker, body in picked:
        lines.append(f"### {ts}" + (f" [{marker}]" if marker else ""))
        lines.append("")
        lines.append(body)
        lines.append("")
    return lines


def _spec_notes(parsed: list[tuple[str, str, str]]) -> list[str]:
    """PO/secretary/steward comments, rendered right under the spec. The card description is not
    editable in this pipeline (`pipeline update` has no description flag), so a comment is the only
    way the PO can amend a spec after creation — which makes these strictly newer than the text
    above them, and the reviewer has to be told that rather than left to guess which wins."""
    picked = [p for p in parsed if p[1] in card_comments.OPERATOR_MARKERS]
    if not picked:
        return []
    dropped = len(picked) - _SPEC_NOTE_LIMIT
    lines = [
        "### Spec amendments in comments",
        "",
        "The card description above is not edited after creation, so the PO amends the spec by "
        "comment. These comments are newer than the description: where they disagree, the comment "
        "defines the criterion and the description is stale on that point. Direct instructions to "
        "the reviewer here are also binding.",
        "",
    ]
    if dropped > 0:
        lines += [f"(the last {_SPEC_NOTE_LIMIT} of {len(picked)}; the rest are in `pipeline show`)", ""]
    return lines + _entries(picked[-_SPEC_NOTE_LIMIT:])


def _rounds(parsed: list[tuple[str, str, str]]) -> list[str]:
    """Verdicts of earlier review rounds on this same card, plus the dispatcher's return notes.

    Without this a red verdict is amnesic by construction: `_review_red` returns the card to In
    progress and the next Validate entry spawns a brand-new head with a brand-new prompt, so round
    N sees the worker's answer to round N-1's finding with no idea a finding existed. That has cost
    a real miss: round 1 flagged a collision risk in a slug rule, the worker replaced the rule, and
    round 2 read a description saying one thing and code saying another, then passed it green as if
    the gap were nothing."""
    picked = [p for p in parsed if p[1] in _ROUND_MARKERS]
    if not picked:
        return []
    dropped = len(picked) - _ROUND_LIMIT
    lines = [
        "## Earlier review rounds on this card",
        "",
        "This card has been reviewed before. Below are the verdicts of earlier rounds and the "
        "dispatcher's reasons for sending it back. This is NOT a description of the current code: "
        "the worker has worked since. For each earlier finding, decide whether it is closed in the "
        "current state. And if the current code disagrees with the spec precisely because an "
        "earlier round demanded it, that is not a new defect but a divergence between spec and "
        "code: state it explicitly in the verdict rather than passing over it silently or "
        "presenting it as a finding.",
        "",
    ]
    if dropped > 0:
        lines += [f"(the last {_ROUND_LIMIT} of {len(picked)}; the rest are in `pipeline show`)", ""]
    return lines + _entries(picked[-_ROUND_LIMIT:])


def _history(parsed: list[tuple[str, str, str]]) -> list[str]:
    """Everything else on the card, newest tail first-class: worker reports, CI verdicts, blocked
    notes. The comments already rendered as spec notes or prior rounds are left out so the two
    sections that matter most are not diluted by their own duplicates."""
    shown = card_comments.OPERATOR_MARKERS | set(_ROUND_MARKERS)
    picked = [p for p in parsed if p[1] not in shown]
    if not picked:
        return []
    dropped = len(picked) - _HISTORY_LIMIT
    lines = ["## The rest of the card's history", ""]
    if dropped > 0:
        lines += [f"(the last {_HISTORY_LIMIT} of {len(picked)}; the rest are in `pipeline show`)", ""]
    return lines + _entries(picked[-_HISTORY_LIMIT:])


def _quality_lens() -> str:
    """The thermo-nuclear skill, read from disk and embedded. Never hardcode its content — read
    the current file so the lens tracks the skill, and degrade to a pointer if it is absent."""
    try:
        return (f"Below is the whole quality module (the thermo-nuclear skill); apply it as it is:\n\n"
                f"````\n{THERMO_SKILL.read_text(encoding='utf-8').strip()}\n````")
    except OSError:
        return (f"The quality module is the thermo-nuclear skill at `{THERMO_SKILL}` "
                f"(it could not be read while building this prompt). Load it yourself and apply it "
                f"as it is.")


def build_task(card: dict, ref: str, pr: str | None, spec: str, base_branch: str,
              branch: str | None = None, head_sha: str | None = None,
              comments: list[dict] | None = None) -> str:
    """REVIEW.md for the reviewer head: what to review, the three lenses, the blocking semantics,
    and how to emit the verdict + proposal cards through board-CLI. `spec` is the card description.
    `pr` is the card's PR link — or None for a contrib (fork) card, which has no PR in this
    pipeline by definition (a human opens it against upstream from the pushed branch afterward);
    `branch`/`head_sha` then point at what to review instead (the worker's own report:done
    protocol line, validate._contrib_ref). `comments` is the card's comment list (ops.show_card),
    rendered into the prompt: the reviewer must not depend on choosing to go fetch its own history
    (see _rounds and _spec_notes)."""
    project = card.get("project", "?")
    parsed = _parse(comments or [])
    review_branch = naming.reviewer_branch(ref)
    if pr:
        what_to_read = [
            f"The card's PR: {pr}",
            f"The worker's report (report:done) and the whole card history are pasted below as "
            f"their own sections — read them there instead of guessing that you should go and "
            f"fetch them. The report names the live checks that were claimed (see below). For the "
            f"full text of anything marked as clipped: "
            f"`python3 -m triggered_agents pipeline show --ref {ref}`, no role needed.",
            f"The project's base branch: `{base_branch}`.",
            f"The workspace already sits on the PR's state — your own branch `{review_branch}` was "
            "cut from the PR head at launch, so there is nothing to check out or switch. You have "
            "the whole repository and the full PR, not only the diff:",
            "```",
            f"gh pr diff {pr}       # the PR diff, when you specifically want a diff rather than the code",
            "```",
            f"Read the repository files at the PR's state, not only the diff lines. Your branch "
            f"`{review_branch}` is only your working copy; do not push it, or any branch.",
        ]
    else:
        what_to_read = [
            f"A contrib (fork) card: no PR is opened in this pipeline — a human prepares the branch "
            f"in the fork for the upstream author. The worker's branch: `{branch}`, head: "
            f"`{head_sha}`.",
            f"The worker's report (report:done) and the whole card history are pasted below as "
            f"their own sections — read them there instead of guessing that you should go and "
            f"fetch them. The report names the live checks that were claimed (see below). For the "
            f"full text of anything marked as clipped: "
            f"`python3 -m triggered_agents pipeline show --ref {ref}`, no role needed.",
            f"The project's base branch: `{base_branch}`.",
            f"The workspace already sits on that branch's state — your own branch `{review_branch}` "
            "was cut from the same head at launch, so there is nothing to check out or switch. You "
            "have the whole repository at this state, not only the diff:",
            "```",
            "git log --oneline -20     # the branch history, when you only want the commit list",
            "```",
            f"Read the repository files at this state, not only the diff lines. Your branch "
            f"`{review_branch}` is only your working copy; do not push it, or any branch.",
        ]
    lines = [
        f"# Review of task {ref} ({project}) — validation layer 3",
        "",
        "You are an independent reviewer head. You are NOT this card's worker and you have no "
        "write access to the code: do not commit, do not push, do not change the PR. Your only "
        "artifacts are one verdict comment and, if needed, idea cards. The lower validation layers "
        "(CI, plus the stand and end-to-end run for stand projects) are already green; your work "
        "sits on top of them.",
        "",
        naming.memory_block("reviewer", project),
        "",
        "## What to read",
        "",
        *what_to_read,
        "",
        "## The card's spec (you check it criterion by criterion)",
        "",
        spec or "(the card description is empty)",
        "",
        *_spec_notes(parsed),
        *_rounds(parsed),
        "## Three lenses (all mandatory)",
        "",
        "### 1. Spec compliance",
        "For each acceptance criterion of the spec, decide whether it is met FOR REAL or ON PAPER "
        "(claimed in the report while the code or test does not do it, the check is mocked all the "
        "way through, the criterion is worked around). Give each one a real/on-paper verdict with "
        "justification from the code.",
        "",
        "### 2. Adversarial bug hunt",
        "Look for defects by failure class; all of these are mandatory:",
        "- **Error paths**: what happens when each stage or call fails (is the exception swallowed? "
        "is state left partial? does it retry forever?).",
        "- **Races and concurrency**: parallel ticks, workers and handles, shared state, lock files, "
        "read-check-write without atomicity.",
        "- **Hanging forever with no signal to a human**: can the card or process stall so that "
        "nobody finds out (no watchdog, no escalation, an infinite retry, a lost handle).",
        "- **Secret leakage** into comments, logs or the board (tokens, env, keys — anything posted "
        "without scrubbing).",
        "- **Blast radius on neighbouring systems**: does the change touch other containers, "
        "branches, state or board entries outside its own task.",
        "Every finding comes with a specific file and a breakage scenario (which input or state "
        "leads to what going wrong).",
        "",
        "### 3. Code quality (thermo-nuclear)",
        _quality_lens(),
        "",
        *_history(parsed),
        "## Live checks from the worker's report",
        "",
        "A green verdict is now enough for an automatic merge; there is no human read after it. If "
        "the worker's report claims a specific live check (a smoke test, a manual run, a request "
        "against an endpoint, running a script or command), run it yourself where that is safe and "
        "reproducible right in this workspace (no stand, no Docker, no writes to external services "
        "or other people's data). For a heavyweight check a reviewer deliberately should not repeat "
        "(Docker, a stand, an external write), independent mechanical evidence is acceptable: check "
        "that the exact CI or stand job is green on the CURRENT head SHA, read its workflow or "
        "command and the relevant logs or artifacts, and satisfy yourself that the job really "
        "executes the claimed path rather than a mock or a no-op. Such evidence counts as the "
        "criterion actually being met; the absence of a personal Docker run is not by itself a "
        "blocker. If there is neither a safe rerun nor suitable mechanical evidence for the current "
        "SHA, do not guess: record it as on-paper and explain what is missing.",
        "",
        "## Blocking semantics (important — what blocks and what does not)",
        "",
        "- **Blockers** (a red verdict): the PR's own diff undermines code quality and there is a "
        "specific fixable remark; OR a file goes past 1000 lines because of this PR; OR a bug of "
        "any class from lens 2; OR a criterion is met only on paper.",
        "- **NOT blockers**: pre-existing debt, findings in NEIGHBOURING code the PR did not touch, "
        "ambitious rebuilds outside the card. Do NOT push those into a red verdict — file them as "
        "cards in the Issues column (see below). That is the only exception to \"no write access to "
        "the code\".",
        "",
        "## How to deliver the result",
        "",
        "One verdict comment through the board CLI. Red if there is a blocker in any lens; "
        "otherwise green. The verdict body:",
        "1. For each spec criterion: real or on paper, and why.",
        "2. For each live check from the worker's report: ran it yourself (with the outcome), OR "
        "confirmed it by mechanical evidence for the current SHA (which job, which workflow or "
        "command, and the result), OR could not confirm it (why) — do not skip any of them "
        "silently.",
        "3. The findings, ranked as blocker or remark, EACH with a file and a breakage scenario.",
        "",
        "```",
        "# red (there are blockers), the body is mandatory:",
        f"python3 -m triggered_agents pipeline --role reviewer verdict --ref {ref} --kind red --body-file <file>",
        "# green (no blockers):",
        f"python3 -m triggered_agents pipeline --role reviewer verdict --ref {ref} --kind green --body-file <file>",
        "```",
        "",
        "Non-blockers (neighbouring code, debt, ideas beyond the card) go into proposal cards:",
        "```",
        f"python3 -m triggered_agents pipeline --role reviewer idea --project {project} "
        "--title '<short>' --description-file <file>",
        "```",
        "The command picks the column itself. A board whose first column is not Issues has "
        "nowhere to put a proposal, so the call fails with that explanation: then keep the "
        "non-blocker in the verdict body as a remark and create nothing.",
        "",
        "You post EXACTLY ONE verdict. Do not move the card and do not write code: on a red verdict "
        "the dispatcher returns it itself.",
    ]
    return "\n".join(lines)
