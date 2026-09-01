"""The one immutable identity of a review round: which candidate, over which base.

A review round is a question put to a reviewer about one exact pair of revisions, and everything
downstream — the document the reviewer is handed, the verdict it publishes, the park that verdict
earns, the merge that follows a release — has to be talking about that same pair. Before this
module the pair was reconstructed at each of those points from whatever mechanical evidence
happened to be on the record, and the mechanical evidence is mutable: a dispatcher-owned gate
receipt is replaced at the assessment and release stages, and its base moves whenever the base
branch does. A verdict compared against the receipt standing at the time of comparison therefore
stopped matching its own round the moment a later stage overwrote it.

So the pair is bound once, here, as one typed value, and the receipt goes back to being what it
is: mechanical evidence for its own stage. ``ReviewRoundContext`` also carries the round it was
bound for, because a context that outlived its round is not a fact about the current one - a
missing reset has to be visible rather than silently reused.

The persisted field has three states, not two, and this module is where they stay apart. Absence
is a round nobody has opened, and only there may the recovery precedence below establish a pair.
Damage - a half-written value, an unknown source, a payload that is not an object at all - is a
round whose identity was *lost*, and it is preserved as ``DamagedReviewContext`` rather than read
back as absence, because a reviewer may already have answered the round it names: rebinding over
it would invent an identity for a verdict that already exists. Both ``bind_review_context`` and
``require_review_context`` refuse it, so every path that reaches this boundary - reviewer bring-up,
launch-intent adoption, verdict selection, merge readiness for the park, the observer release and
the no-observer release alike - fails closed on the board with the reason instead of merging.

Nothing in this module reads a verdict, decides whether the checkout has since drifted off the
bound candidate, or attests a broad suite. Drift has two owners already — the reviewer bring-up,
which refuses to hand a pane a candidate the checkout no longer holds, and the merge readiness
check, which bounces a verdict whose code state has moved — and a third opinion here would only
disagree with them. The context is identity; a dispatcher-owned exact-SHA gate receipt remains the
only thing that says a check was executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from secretary.dispatcher_types import HostError

if TYPE_CHECKING:
    from secretary.dispatcher_gate_receipt import GateReceipt
    from secretary.dispatcher_state import DispatcherRecord

_EXACT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Where a bound pair came from. Recorded so an operator reading a blocked card can tell a round
# recovered from its own durable launch from one resolved without any attestation at all.
REVIEW_CONTEXT_SOURCES = ("initial-receipt", "recorded-launch", "resolved-unattested")


class ReviewContextError(HostError):
    """This review round's candidate/base identity is missing, partial or contradictory.

    A subclass of ``HostError`` so the launch and adoption paths that already end a tick on a host
    refusal keep doing so, and distinct from it so the dispatcher can answer this one with the
    lifecycle evidence it deserves instead of a generic bring-up failure.
    """


@dataclass(frozen=True, slots=True)
class ReviewRoundContext:
    """The exact pair one review round was opened on, plus the identity of that round."""

    candidate_sha: str
    base_sha: str
    attempt_id: str
    review_baseline: int
    source: str

    def __post_init__(self) -> None:
        if not _EXACT_SHA_RE.fullmatch(self.candidate_sha):
            raise ValueError("review context candidate must be an exact lowercase 40-hex commit")
        if not _EXACT_SHA_RE.fullmatch(self.base_sha):
            raise ValueError("review context base must be an exact lowercase 40-hex commit")
        if not self.attempt_id:
            raise ValueError("review context must name the attempt it was bound in")
        if self.review_baseline < 0:
            raise ValueError("review context must name a non-negative review baseline")
        if self.source not in REVIEW_CONTEXT_SOURCES:
            raise ValueError("review context source must be one of " + ", ".join(REVIEW_CONTEXT_SOURCES))

    def names_round(self, attempt_id: str, review_baseline: int) -> bool:
        """Whether this context is the identity of the round the record is currently in."""
        return self.attempt_id == attempt_id and self.review_baseline == review_baseline

    def names_revisions(self, candidate_sha: str, base_sha: str) -> bool:
        """Whether a verdict's header describes exactly the pair this round was opened on."""
        return (self.candidate_sha, self.base_sha) == (candidate_sha, base_sha)

    def to_json(self) -> dict[str, Any]:
        return {
            "candidate_sha": self.candidate_sha,
            "base_sha": self.base_sha,
            "attempt_id": self.attempt_id,
            "review_baseline": self.review_baseline,
            "source": self.source,
        }

    @classmethod
    def from_json(cls, payload: Any) -> ReviewRoundContext | None:
        """Read a persisted context. Absence is "no round is bound"; damage is not.

        A half-readable context is refused rather than dropped: dropping it would let the next
        bind invent a different pair for a round a reviewer is already answering.
        """
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise TypeError("persisted review context must be an object")
        return cls(
            candidate_sha=str(payload.get("candidate_sha") or ""),
            base_sha=str(payload.get("base_sha") or ""),
            attempt_id=str(payload.get("attempt_id") or ""),
            review_baseline=int(payload.get("review_baseline") or 0),
            source=str(payload.get("source") or ""),
        )


@dataclass(frozen=True, slots=True)
class DamagedReviewContext:
    """A persisted review context that could not be read back as one.

    Absence and damage are different facts and must not collapse into the same value. "No context"
    is a round that has not been opened yet, and the documented recovery precedence may establish
    one. A half-written or contradictory context is a round whose identity was lost, and a reviewer
    may already be answering it: recovering a pair for it would invent an identity for a verdict
    somebody else has already given. So the damage is kept, with the reason, and it is kept
    *durably* - ``to_json`` hands back the exact payload that was read, so a save does not quietly
    launder the damage into an absence the next tick would happily rebind over. Only a round
    ending - which is what ``open_review_round`` is - clears it.
    """

    reason: str
    payload: Any

    def to_json(self) -> Any:
        return self.payload


def load_review_context(payload: Any) -> ReviewRoundContext | DamagedReviewContext | None:
    """Read a persisted review context into one of its three states.

    Absence stays absence, a complete value becomes the typed context, and anything else becomes
    the damage itself rather than a traceback that would unload the whole record over one field.
    """
    try:
        return ReviewRoundContext.from_json(payload)
    except (TypeError, ValueError) as exc:
        return DamagedReviewContext(reason=str(exc), payload=payload)


def open_review_round(record: DispatcherRecord, review_baseline: int) -> None:
    """Move the record onto a new review round: its baseline and its identity, in one step.

    Every lifecycle step that ends a round — the worker report that opens the next one, the red
    transition that hands the checkout back, the stale-done bounce, the infrastructure retry —
    advances the baseline that says where this round's verdict is read from. The context of the
    round that just ended goes with it, here, so there is no window in which a new round's
    baseline stands beside the previous round's candidate. Half a context, or a context belonging
    to another round, is never a state this record can be left in.
    """
    record.review_context = None
    record.review_baseline = review_baseline


def bind_review_context(
    host: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    *,
    receipt: GateReceipt | None = None,
    unattested: bool = False,
    recorded_launch: bool = False,
) -> ReviewRoundContext:
    """Bind, or re-confirm, the immutable candidate/base identity of the current review round.

    The recovery precedence is the whole contract:

    1. the context already bound for this round, which no later evidence may replace;
    2. the durable launch this dispatcher recorded for this round, when the caller has proved
       there was one (``recorded_launch``) - the reviewer's own generated verdict commands;
    3. the applicable initial exact-SHA gate receipt, passed by the one caller that owns it;
    4. the pinned checkout with a freshly resolved base, and only when the caller says this
       round's gate is explicitly non-attesting (``unattested``);
    5. otherwise nothing is bound and this raises, because a review round whose identity cannot
       be established is a round that must not start, produce a verdict or release one.

    Later gate receipts are deliberately not an input here. They belong to their own stage and may
    legitimately name a base this round never saw.
    """
    damaged = _damage(record)
    if damaged is not None:
        # Not a round that can be opened: a round whose identity was lost. Nothing here may
        # replace it, because the pair it lost is the pair a reviewer may already be answering.
        raise damaged
    recovered = _recorded_pair(host, task, record) if recorded_launch else None
    existing = record.review_context
    if existing is not None:
        _require_current_round(existing, record)
        if recovered is not None and not existing.names_revisions(*recovered):
            raise ReviewContextError(
                "the recorded reviewer launch names a different candidate/base than the bound round"
            )
        return existing
    if recovered is not None:
        candidate, base, source = recovered[0], recovered[1], "recorded-launch"
    elif receipt is not None:
        # Already accepted against the pinned checkout by the gate policy that produced it; this
        # never re-derives a receipt of its own from persisted state.
        candidate, base, source = receipt.validated_sha, receipt.base_sha, "initial-receipt"
    elif unattested:
        candidate = str(host.head_commit(record) or "")
        base = str(host.review_base_commit(task, record) or "")
        source = "resolved-unattested"
    else:
        raise ReviewContextError(
            "no recorded reviewer launch and no initial exact-SHA receipt name this review round"
        )
    try:
        context = ReviewRoundContext(
            candidate_sha=candidate.lower(),
            base_sha=base.lower(),
            attempt_id=record.attempt_id,
            review_baseline=record.review_baseline,
            source=source,
        )
    except ValueError as exc:
        raise ReviewContextError(f"review context cannot be bound: {exc}") from None
    record.review_context = context
    return context


def require_verdict_review_context(
    host: Any, task: dict[str, Any], record: DispatcherRecord
) -> ReviewRoundContext:
    """The identity a standing verdict is read against: required, never established.

    Once a reviewer has published a verdict, the pair that verdict judged is a fact fixed before
    it, so this deliberately has no recovery precedence at all - a missing, damaged or foreign
    context is a refusal, not an invitation to derive a pair that would fit the answer already on
    the board. The round's own recorded launch is still read, but only as a contradiction check:
    it can disqualify the bound context, never supply one.
    """
    context = require_review_context(record)
    recovered = _recorded_pair(host, task, record)
    if recovered is not None and not context.names_revisions(*recovered):
        raise ReviewContextError(
            "the recorded reviewer launch names a different candidate/base than the bound round"
        )
    return context


def require_review_context(record: DispatcherRecord) -> ReviewRoundContext:
    """The bound context of the round the record is in, or a fail-closed refusal.

    Every read path goes through this rather than through the field, so "no context" and "a
    context from a round that has ended" are one answer with one message at every site.
    """
    damaged = _damage(record)
    if damaged is not None:
        raise damaged
    context = record.review_context
    if context is None:
        raise ReviewContextError("this review round has no bound candidate/base context")
    _require_current_round(context, record)
    return context


def _damage(record: DispatcherRecord) -> ReviewContextError | None:
    """The refusal a damaged persisted context owes, or ``None`` when there is no damage."""
    context = record.review_context
    if not isinstance(context, DamagedReviewContext):
        return None
    return ReviewContextError("the persisted context of this review round is unreadable: " + context.reason)


def _require_current_round(context: ReviewRoundContext, record: DispatcherRecord) -> None:
    """A context that outlived its round is a missing reset, not a fact about the current one."""
    if not context.names_round(record.attempt_id, record.review_baseline):
        raise ReviewContextError(
            "the bound review context belongs to review round "
            f"{context.review_baseline} of attempt {context.attempt_id}, "
            f"not to round {record.review_baseline} of attempt {record.attempt_id}"
        )


def _recorded_pair(host: Any, task: dict[str, Any], record: DispatcherRecord) -> tuple[str, str] | None:
    """The pair this dispatcher wrote into the round's own reviewer document, when it survives."""
    recovered = host.recorded_review_context(task, record)
    if recovered is None:
        return None
    candidate, base = recovered
    return str(candidate).lower(), str(base).lower()
