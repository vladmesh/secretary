"""Deterministic inspection of a candidate branch's own commit messages (secretary-1401).

The pipeline's git history is a published artefact: once a worker's commits reach `origin` the
ordinary repair — amend, rebase, re-report — is no longer available, and the only remaining fixes
are the two this product refuses to automate, a rewrite of published history and a force push. So
the one check that has to happen *before* publication is the one over what the commits say.

In `sprint:1300` a worker published two commits carrying `Co-Authored-By:` AI trailers. Nothing
mechanical looked: the instruction not to add AI co-authorship lived only in the codex home's
`AGENTS.md`, so it never reached a head of another family, and the reviewer read the diff after the
push. This module is the deterministic half of the repair. It reads commit messages and nothing
else — no diff, no working tree, no network — so the same candidate always gets the same answer,
and it names the commits rather than repairing them: a rewrite is the worker's own local step.

The identity list is deliberately narrow. It catches the trailers the coding agents in this
pipeline actually write and the well-known ones next to them; it does not try to recognise "an AI"
in general, because the cost of a false positive is a green candidate bounced back to a worker over
a human collaborator's name. Ordinary human co-authorship is a legitimate trailer and stays legal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# A git trailer, as `git interpret-trailers` writes it and as every runtime here emits it. Matched
# per line and case-insensitively: `Co-authored-by`, `CO-AUTHORED-BY` and a leading-space variant
# are the same trailer, and a candidate that hides one behind different casing is exactly what a
# deterministic check is for.
_TRAILER_RE = re.compile(r"(?im)^[ \t]*co-authored-by[ \t]*:[ \t]*(?P<identity>.+?)[ \t]*$")

# The AI identities this preflight rejects, matched as whole words inside the trailer's identity
# (its display name *and* its address). The boundaries are what keep a human out of the net: a
# co-author called "Claudia" or writing from `codexpert.example` shares a prefix with an entry here
# and must stay legal, so an entry only fires when it is not glued to another letter or digit.
_AI_IDENTITIES = (
    "claude",
    "anthropic",
    "codex",
    "openai",
    "chatgpt",
    "copilot",
    "cursor",
    "devin",
    "gemini",
)
_AI_IDENTITY_RE = re.compile(
    r"(?i)(?<![a-z0-9])(" + "|".join(_AI_IDENTITIES) + r")(?![a-z0-9])"
)


@dataclass(frozen=True)
class Commit:
    """One candidate commit, as `git log` hands it over: object id and full message."""

    sha: str
    message: str

    @property
    def subject(self) -> str:
        return self.message.strip().splitlines()[0].strip() if self.message.strip() else ""


@dataclass(frozen=True)
class AiAttribution:
    """One forbidden trailer, with the commit it would have published."""

    sha: str
    subject: str
    trailer: str
    identity: str

    def render(self) -> str:
        short = self.sha[:12] or "(unknown)"
        return f"{short} {self.subject}: Co-Authored-By: {self.trailer} [{self.identity}]"


def ai_attributions(commits: Iterable[Commit]) -> list[AiAttribution]:
    """Every AI co-author trailer in `commits`, in the order the commits were given.

    A commit with two forbidden trailers reports both: the repair is per line, and a message that
    named only the first would send the worker back for a second round over the second.
    """
    found: list[AiAttribution] = []
    for commit in commits:
        for match in _TRAILER_RE.finditer(commit.message or ""):
            identity = match.group("identity").strip()
            hit = _AI_IDENTITY_RE.search(identity)
            if hit is None:
                continue
            found.append(
                AiAttribution(
                    sha=commit.sha,
                    subject=commit.subject,
                    trailer=identity,
                    identity=hit.group(1).lower(),
                )
            )
    return found


def parse_log(text: str) -> list[Commit]:
    """Read `git log --format=%H%x1f%B%x1e` output. Tolerant of a trailing record separator."""
    commits: list[Commit] = []
    for record in (text or "").split("\x1e"):
        if not record.strip():
            continue
        sha, _, message = record.strip("\n").partition("\x1f")
        commits.append(Commit(sha=sha.strip(), message=message))
    return commits


def repair_message(attributions: list[AiAttribution], base: str) -> str:
    """What the worker is told to do about it. A local repair, spelled out, and nothing else.

    No command here rewrites anything on its own and none of them touches `origin`: this runs
    before the candidate is published precisely so the repair stays inside the worker's own
    checkout, and a candidate that somehow reached the remote is a report, not a force push.
    """
    listing = "\n".join(f"  - {item.render()}" for item in attributions)
    return (
        "These commits on your branch carry forbidden AI co-author trailers:\n"
        f"{listing}\n\n"
        "Nothing has been published and nothing was rewritten for you. Repair the messages in your "
        "own checkout, then report done again:\n"
        f"  - a single commit at the tip: `git commit --amend` and delete the `Co-Authored-By:` line;\n"
        f"  - more than one: `git rebase -i origin/{base}` and reword each commit listed above.\n"
        "Do not add AI co-authorship again, in any model family's format. Ordinary human "
        "co-authors are fine and must stay. If any listed commit is already on the remote, do not "
        "force-push: report blocked and name it."
    )
