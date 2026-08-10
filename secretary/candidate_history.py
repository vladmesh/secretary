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

A commit message is untrusted input. It is written by the head under review, it may contain any
byte git accepts including control characters, and a candidate that wants to hide a trailer will
put whatever it takes in it. Two consequences run through this module:

  * Nothing here frames records inside message text. The caller reads object ids first, from a
    format that cannot be forged, and then one message per id; there is no delimiter a message can
    contain to break the parse into a shape that reads as "no trailers" (`parse_shas` refuses
    anything that is not an object id, rather than skipping it).
  * Addressed identities are compared to an exact registry of name/address pairs. Neither a model
    vendor's domain nor an ambiguous local part is evidence on its own: both can belong to a human.
    A trailer with no address is compared against exact agent names rather than searched for a word.

The identity lists are deliberately narrow. They catch the agents this pipeline actually runs and
the well-known ones next to them; they do not try to recognise "an AI" in general, because the cost
of a false positive is a green candidate bounced back to a worker over a colleague's name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# A git trailer, as `git interpret-trailers` writes it and as every runtime here emits it. Matched
# per line and case-insensitively: `Co-authored-by`, `CO-AUTHORED-BY` and a leading-space variant
# are the same trailer, and a candidate that hides one behind different casing is exactly what a
# deterministic check is for.
_TRAILER_RE = re.compile(r"(?i)^[ \t]*co-authored-by[ \t]*:[ \t]*(?P<identity>.+?)[ \t]*$")
_ANY_TRAILER_RE = re.compile(r"^[ \t]*[A-Za-z0-9-]+[ \t]*:[ \t]*.+?[ \t]*$")

# `Display Name <local@domain>`, the only shape git itself writes for an identity.
_IDENTITY_RE = re.compile(r"^(?P<name>.*?)\s*<(?P<address>[^<>]*)>\s*$")

# Domains whose mail is a model vendor's own. An address here is not a colleague's: it is the
# vendor's agent account, which is what `noreply@anthropic.com` was on the two published commits.
_REGISTERED_AGENT_IDENTITIES = frozenset({
    ("claude", "noreply@anthropic.com"),
    ("claude code", "noreply@anthropic.com"),
    ("codex", "codex@openai.com"),
    ("openai codex", "codex@openai.com"),
    ("github copilot", "copilot@users.noreply.github.com"),
    ("copilot", "copilot@users.noreply.github.com"),
})

# Exact identities for the malformed trailer git would never write itself: `Co-Authored-By: Claude`
# with no address. Compared whole, after collapsing whitespace and casing, so "Claude Martin" —
# a person — is not one of them.
_AGENT_NAMES = frozenset({
    "aider",
    "anthropic",
    "chatgpt",
    "claude",
    "claude code",
    "codex",
    "copilot",
    "cursor",
    "devin",
    "gemini",
    "github copilot",
    "openai",
    "openai codex",
})

_GITHUB_NOREPLY_ID_RE = re.compile(r"^\d+\+")
_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True)
class Commit:
    """One candidate commit, as git hands it over: object id and full message."""

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


def forbidden_identity(trailer: str) -> str:
    """What makes this co-author an agent, or "" when it is an ordinary human co-author.

    The answer doubles as the evidence line an operator reads, so it names the exact registered
    identity, or the bare agent name of an addressless trailer.
    """
    identity = " ".join(trailer.split())
    match = _IDENTITY_RE.match(identity)
    if match is None:
        # No address to judge. Only an exact agent name counts, never a name that contains one.
        return f"name {identity.lower()}" if identity.lower() in _AGENT_NAMES else ""
    name = " ".join(match.group("name").split()).lower()
    address = match.group("address").strip().lower()
    if "@" not in address:
        # `<claude>` is not an address either; treat what is inside the brackets as a bare name.
        bare = address or " ".join(match.group("name").split()).lower()
        return f"name {bare}" if bare in _AGENT_NAMES else ""
    canonical_address = _GITHUB_NOREPLY_ID_RE.sub("", address)
    if (name, canonical_address) in _REGISTERED_AGENT_IDENTITIES:
        return f"registered agent {name} <{canonical_address}>"
    return ""


def _final_trailer_lines(message: str) -> list[str]:
    """Return only the terminal Git trailer block, never trailer-shaped prose in the body."""
    lines = (message or "").rstrip().splitlines()
    trailers: list[str] = []
    start = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if not _ANY_TRAILER_RE.match(line):
            break
        trailers.append(line)
        start = index
    if not trailers or (start > 0 and lines[start - 1].strip()):
        return []
    trailers.reverse()
    return trailers


def ai_attributions(commits: Iterable[Commit]) -> list[AiAttribution]:
    """Every AI co-author trailer in `commits`, in the order the commits were given.

    A commit with two forbidden trailers reports both: the repair is per line, and a message that
    named only the first would send the worker back for a second round over the second.
    """
    found: list[AiAttribution] = []
    for commit in commits:
        for line in _final_trailer_lines(commit.message):
            match = _TRAILER_RE.match(line)
            if match is None:
                continue
            trailer = match.group("identity").strip()
            identity = forbidden_identity(trailer)
            if not identity:
                continue
            found.append(
                AiAttribution(
                    sha=commit.sha,
                    subject=commit.subject,
                    trailer=trailer,
                    identity=identity,
                )
            )
    return found


def parse_shas(text: str) -> list[str]:
    """Object ids from `git log --format=%H`, or `ValueError` if the output is not exactly that.

    This is the only thing read out of a candidate-controlled command whose output a candidate
    could try to shape, and it is read strictly for that reason: `%H` is git's own rendering of the
    commit's object id, nothing a message can contain reaches this stream, and a line that is not
    an object id means the assumption behind this check does not hold. That fails the gate rather
    than dropping the line, because a skipped line here is an unchecked commit.
    """
    shas: list[str] = []
    for line in (text or "").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if not _OBJECT_ID_RE.match(candidate):
            raise ValueError(f"unexpected line in the candidate's object id listing: {candidate!r}")
        shas.append(candidate)
    return shas


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
