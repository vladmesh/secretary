"""The one authority on the pair a verdict effect would actually land on: the current pair.

A review round is judged over one exact candidate over one exact base, and that identity is sealed
elsewhere. This module answers the other half of the same question, the half that changes while the
card waits: what the checkout and the base branch are *right now*. Nothing may be moved or merged
until both halves are known exactly, because the whole point of the comparison is that a verdict
which no longer describes the code is not a verdict any effect may act on.

The defect this module exists for was not a missing check. It was a *readable* one whose inputs
were strings: the executor read the checkout with ``head_commit`` and the base with
``review_base_commit``, both of which answer ``""`` when the host cannot run Git — a noop host, a
workspace that is gone, a `git` that failed, a rev-parse that printed nothing. The drift comparison
then saw an empty candidate, found no mismatch, and let the effect through. An unreadable workspace
and an intact one produced the same value, so on first execution and on every crash replay a card
could be parked, gated and merged over a pair nobody had read.

So the answer is typed, and the two states are different types rather than two spellings of one
string:

* ``ResolvedCurrentPair`` is a success. It carries the exact normalized candidate and base, and the
  ancestry facts the drift and readiness checks need: whether the candidate is still the reviewed
  one, whether a differing one is the supported instance-publish recovery, and how the base branch
  now stands to the base this round was judged over. It is sealed, so only ``resolve_current_pair``
  can issue one and no call site can write a plausible-looking pair out longhand.
* ``UnresolvedCurrentPair`` is every other outcome, each named: the host cannot address the
  workspace at all, a command failed, the output was empty, it was not an exact object id, it named
  more than one revision, or the ancestry question could not be decided. None of them is an empty
  string, none of them compares equal to anything, and none of them can enter an effect's
  preconditions.

The comparison against the sealed verdict identity is deliberately *not* performed here.
Resolution answers what is true of the workspace; the executor decides what that means for the
effect it is about to perform, and it is the one place that decision lives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from secretary.dispatch.review_context import ValidatedReviewIdentity
    from secretary.dispatcher_state import DispatcherRecord

_EXACT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Why a current pair could not be resolved. One name per way the answer can fail to be an exact
# pair, because an operator reading a blocked card has to be able to tell "this host cannot see the
# workspace" from "the base branch was rewritten", and because a repair differs for each.
CURRENT_PAIR_UNAVAILABLE = "unavailable"
CURRENT_PAIR_FAILED = "failed"
CURRENT_PAIR_EMPTY = "empty"
CURRENT_PAIR_MALFORMED = "malformed"
CURRENT_PAIR_AMBIGUOUS = "ambiguous"
CURRENT_PAIR_ANCESTRY = "ancestry"
CURRENT_PAIR_OUTCOMES = (
    CURRENT_PAIR_UNAVAILABLE,
    CURRENT_PAIR_FAILED,
    CURRENT_PAIR_EMPTY,
    CURRENT_PAIR_MALFORMED,
    CURRENT_PAIR_AMBIGUOUS,
    CURRENT_PAIR_ANCESTRY,
)

# How the base branch as it is now stands to the base this round was judged over. `advanced` is the
# ordinary case a release must not refuse — other cards land while this one waits — and `rewritten`
# is a history that no longer contains the delta the reviewer read.
BASE_IDENTICAL = "identical"
BASE_ADVANCED = "advanced"
BASE_REWRITTEN = "rewritten"


@dataclass(frozen=True, slots=True)
class RevisionRead:
    """One raw revision read, exactly as the host produced it. No interpretation, no sentinel.

    ``available`` is whether this host can address the workspace at all; ``ok`` is whether the
    command it ran exited cleanly; ``output`` is that command's stdout, unmodified, including the
    empty string a rev-parse can legitimately print. Classifying those is the resolver's job below,
    and keeping the classification out of the host is what stops a second reading of "no output"
    from growing somewhere else.
    """

    available: bool
    ok: bool
    output: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AncestryRead:
    """Whether one commit is reachable from another, as a decision or as the absence of one.

    ``contains`` is ``None`` when the question could not be decided — a commit the workspace does
    not have, a repository that could not be opened, a `git` that failed. That is not "no", and it
    is emphatically not "yes": an undecided ancestry is a typed failure, because both answers to it
    are consequential and neither is safe to assume.
    """

    available: bool
    contains: bool | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class UnresolvedCurrentPair:
    """Why this invocation has no current pair. A value, never an exception and never a sentinel.

    Carried back to the executor whole, so the durable evidence a blocked or bounced card is given
    names the specific failure rather than a generic refusal. Every one of these outcomes means the
    same thing about effects — none may be performed — and different things to the operator who has
    to repair it.
    """

    outcome: str
    subject: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in CURRENT_PAIR_OUTCOMES:
            raise ValueError(f"unknown current-pair outcome {self.outcome}")

    @property
    def evidence(self) -> str:
        """One sentence naming what could not be resolved and why, for the card the effect refuses."""
        reason = {
            CURRENT_PAIR_UNAVAILABLE: "this host cannot read the workspace it would be resolved in",
            CURRENT_PAIR_FAILED: "the command that reads it failed",
            CURRENT_PAIR_EMPTY: "the command that reads it produced no output",
            CURRENT_PAIR_MALFORMED: "what it produced is not an exact commit id",
            CURRENT_PAIR_AMBIGUOUS: "what it produced names more than one revision",
            CURRENT_PAIR_ANCESTRY: "the history it stands in could not be decided",
        }[self.outcome]
        detail = f": {self.detail}" if self.detail else ""
        return f"the current {self.subject} could not be resolved: {reason}{detail}"


# The one issuer of a resolved pair. Private for the same reason the review identity's seal is: an
# effect's preconditions ask for this type, and the type can only come from the resolution that
# established it.
_RESOLVER_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class ResolvedCurrentPair:
    """The checkout and the base branch as they are now, exactly, with the ancestry facts.

    Only ``resolve_current_pair`` can build one, and only one of these may enter an effect's
    preconditions. That is the whole enforcement: "we read the pair and it is exactly this" and "we
    could not read the pair" are different types, so no comparison can succeed by default and no
    replay can reach a board move, a stage gate or a merge on an answer nobody obtained.

    The facts are stated, not judged. ``candidate_matches`` and ``base_relation`` say what is true
    of this workspace; whether that permits the effect is the executor's decision, made against the
    sealed verdict identity in one place.
    """

    candidate_sha: str
    base_sha: str
    candidate_matches: bool
    publish_recovery: bool
    base_relation: str
    seal: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.seal is not _RESOLVER_AUTHORITY:
            raise ValueError("a current pair is resolved only by resolve_current_pair")
        if not _EXACT_SHA_RE.fullmatch(self.candidate_sha) or not _EXACT_SHA_RE.fullmatch(self.base_sha):
            raise ValueError("a resolved current pair names two exact lowercase 40-hex commits")
        if self.base_relation not in (BASE_IDENTICAL, BASE_ADVANCED, BASE_REWRITTEN):
            raise ValueError(f"unknown base relation {self.base_relation}")

    @property
    def candidate_intact(self) -> bool:
        """Whether the checkout still holds the candidate the verdict was given for.

        The supported instance-publish recovery is the one documented way a differing checkout is
        still this round's candidate: the dispatcher's own publication moved the workspace, and the
        commit the reviewer read is the one that was published.
        """
        return self.candidate_matches or self.publish_recovery

    @property
    def base_intact(self) -> bool:
        """Whether the base branch still contains the base this round was judged over."""
        return self.base_relation in (BASE_IDENTICAL, BASE_ADVANCED)


def resolve_current_pair(
    host: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    identity: ValidatedReviewIdentity,
) -> ResolvedCurrentPair | UnresolvedCurrentPair:
    """Read the checkout and the base branch as they are now, or say exactly why they cannot be.

    Called by every executor invocation, after the round's identity has been sealed and before any
    stage policy is consulted — the sealed identity is what the ancestry question is asked about,
    and stage policy is a decision about a pair that has already been resolved.

    Three reads, in order, and any one of them may end this in a typed non-success:

    1. the checkout's own commit, which must be one exact object id;
    2. the base branch as it is now, refreshed from the remote by the host, likewise exact;
    3. and, when that base is not the base this round was judged over, whether the reviewed base is
       still reachable from it.

    Only the ancestry read is conditional, and only because a base that did not move needs no
    query to be known to contain itself. Nothing here is defaulted, coerced or excused: an empty
    output, a `git` that failed, a ref that resolved to two revisions and a workspace this host
    cannot address are four different answers, and not one of them is a pair.
    """
    candidate = _revision(host.read_head_revision(record), "candidate")
    if isinstance(candidate, UnresolvedCurrentPair):
        return candidate
    base = _revision(host.read_base_revision(task, record), "base")
    if isinstance(base, UnresolvedCurrentPair):
        return base
    relation = _base_relation(host, record, identity.base_sha, base)
    if isinstance(relation, UnresolvedCurrentPair):
        return relation
    matches = candidate == identity.candidate_sha
    return ResolvedCurrentPair(
        candidate_sha=candidate,
        base_sha=base,
        candidate_matches=matches,
        # Asked only about a checkout that actually differs, and answered by the host's own
        # publication evidence. A host that cannot answer it says no, which refuses the effect.
        publish_recovery=(
            False
            if matches
            else bool(host.is_instance_publish_recovery(task, record, identity.candidate_sha, candidate))
        ),
        base_relation=relation,
        seal=_RESOLVER_AUTHORITY,
    )


def _revision(read: RevisionRead, subject: str) -> str | UnresolvedCurrentPair:
    """One raw read, classified into an exact revision or the named reason it is not one.

    The order matters and is the order the failures actually happen in. A host that cannot address
    the workspace never ran a command, so it did not fail one. A command that failed printed
    whatever it printed and none of it is evidence. Only then is the output itself read, and it is
    read strictly: no output is not a revision, two revisions are not a revision, and anything
    other than a 40-hex object id is a ref name, an error message or a truncation — never a commit.
    """
    if not read.available:
        return UnresolvedCurrentPair(CURRENT_PAIR_UNAVAILABLE, subject, read.detail)
    if not read.ok:
        return UnresolvedCurrentPair(CURRENT_PAIR_FAILED, subject, read.detail)
    revisions = read.output.split()
    if not revisions:
        return UnresolvedCurrentPair(CURRENT_PAIR_EMPTY, subject)
    if len(revisions) > 1:
        return UnresolvedCurrentPair(
            CURRENT_PAIR_AMBIGUOUS, subject, f"{len(revisions)} revisions were printed"
        )
    revision = revisions[0].lower()
    if not _EXACT_SHA_RE.fullmatch(revision):
        return UnresolvedCurrentPair(CURRENT_PAIR_MALFORMED, subject)
    return revision


def _base_relation(
    host: Any, record: DispatcherRecord, reviewed_base: str, current_base: str
) -> str | UnresolvedCurrentPair:
    """How the base branch now stands to the base this round was judged over.

    A base that did not move contains itself and is answered without asking Git anything. A base
    that did move is the ordinary case only while the reviewed base is still reachable from it; a
    base branch that was reset, rewritten or replaced leaves the reviewed delta describing a
    history that no longer exists.

    An undecided answer is neither of those and is not guessed. The previous implementation read an
    unreadable ancestry as "intact", on the argument that a workspace this host cannot interrogate
    is its own failure rather than evidence about the branch — which is true, and is exactly why it
    cannot be an input to a merge. It is a typed failure here, and the effect refuses.
    """
    if reviewed_base == current_base:
        return BASE_IDENTICAL
    read = host.read_base_ancestry(record, reviewed_base, current_base)
    if not read.available:
        return UnresolvedCurrentPair(CURRENT_PAIR_UNAVAILABLE, "base ancestry", read.detail)
    if read.contains is None:
        return UnresolvedCurrentPair(CURRENT_PAIR_ANCESTRY, "base ancestry", read.detail)
    return BASE_ADVANCED if read.contains else BASE_REWRITTEN


__all__ = [
    "BASE_ADVANCED",
    "BASE_IDENTICAL",
    "BASE_REWRITTEN",
    "CURRENT_PAIR_AMBIGUOUS",
    "CURRENT_PAIR_ANCESTRY",
    "CURRENT_PAIR_EMPTY",
    "CURRENT_PAIR_FAILED",
    "CURRENT_PAIR_MALFORMED",
    "CURRENT_PAIR_OUTCOMES",
    "CURRENT_PAIR_UNAVAILABLE",
    "AncestryRead",
    "ResolvedCurrentPair",
    "RevisionRead",
    "UnresolvedCurrentPair",
    "resolve_current_pair",
]
