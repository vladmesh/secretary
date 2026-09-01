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

This module has exactly two entries, and the line between them is the verdict:

*Before* a verdict exists, ``bind_review_context`` may still *establish* a pair, following one
documented recovery precedence. That is the only place recovery is legitimate, because there is
no answer yet for a recovered pair to be fitted to.

*After* a verdict exists, ``validate_post_verdict_identity`` is the single authority every
verdict-driven path goes through, and it establishes nothing. It reads the three durable pieces
of evidence this round left behind — the persisted context, the reviewer document this dispatcher
recorded for the round, and the accepted structured verdict itself — and either issues one
``ValidatedReviewIdentity``, which is the only value a park, a gate or a merge will accept, or
refuses with the specific contradiction. Any source may *disqualify* the bound context; none may
*supply* one. A pair derived once a verdict is on the board is an identity invented to fit an
answer that already exists, and a later mechanical receipt is evidence about its own stage rather
than about this round.

The persisted field has three states, not two, and the decoder below is total over every value
the state repository's JSON writer can hand back. Absence is a round nobody has opened, and only
there may the recovery precedence establish a pair. Damage - a half-written value, an unknown
field, a baseline that is a boolean or a non-finite number, a payload that is not an object at
all - is a round whose identity was *lost*, and it is preserved as ``DamagedReviewContext`` with
the reason and the original payload rather than read back as absence or raised as an exception
that would unload the whole production record over one field.

Nothing in this module reads a gate result, decides whether the checkout has since drifted off
the bound candidate, or attests a broad suite. Drift has two owners already — the reviewer
bring-up, which refuses to hand a pane a candidate the checkout no longer holds, and the merge
readiness check, which bounces a verdict whose code state has moved — and a third opinion here
would only disagree with them. The context is identity; a dispatcher-owned exact-SHA gate receipt
remains the only thing that says a check was executed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from secretary.dispatcher_types import HostError

if TYPE_CHECKING:
    from secretary.board.events import VerdictProjection
    from secretary.dispatcher_gate_receipt import GateReceipt
    from secretary.dispatcher_state import DispatcherRecord

_EXACT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Where a bound pair came from. Recorded so an operator reading a blocked card can tell a round
# recovered from its own durable launch from one resolved without any attestation at all.
REVIEW_CONTEXT_SOURCES = ("initial-receipt", "recorded-launch", "resolved-unattested")

# Exactly the fields a persisted context carries: no more, no fewer. An unknown field is a record
# written by something this dispatcher does not understand, and a missing one is half a context.
REVIEW_CONTEXT_FIELDS = ("candidate_sha", "base_sha", "attempt_id", "review_baseline", "source")

# A review baseline counts comments on one card. The bound is not a policy about how many rounds a
# card may have; it is the point past which a persisted number is evidence of corruption rather
# than of a card, and it keeps an arbitrarily large integer from reaching arithmetic or a slice.
MAX_REVIEW_BASELINE = 2**31 - 1


class ReviewContextError(HostError):
    """This review round's candidate/base identity is missing, partial or contradictory.

    A subclass of ``HostError`` so the launch and adoption paths that already end a tick on a host
    refusal keep doing so, and distinct from it so the dispatcher can answer this one with the
    lifecycle evidence it deserves instead of a generic bring-up failure.
    """


class _ContextDamage(ValueError):
    """One specific reason a persisted payload is not a review context. Never leaves the decoder."""


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
    """Read a persisted review context. Total over every JSON-shaped value, by construction.

    The state repository writes records with the standard-library JSON encoder and reads them back
    with its decoder, so the values that can arrive here are exactly ``None``, booleans, integers,
    floats — including the non-finite ones that encoder emits and that decoder accepts — strings,
    arrays and objects, at any nesting. Every one of them lands in one of three answers and none of
    them escapes as an exception:

    * ``None`` is absence, the one state a fresh round may still be established from;
    * a complete, exactly-shaped object is the typed context;
    * anything else is ``DamagedReviewContext``, carrying the specific refusal reason and the
      original payload, so the damage survives the next save instead of being laundered into an
      absence the next tick would rebind over.

    Nothing is coerced on the way: a string is not parsed into a number, a number is not rounded
    into a baseline, a boolean is not read as the integer Python says it also is, and a revision is
    not case-folded into shape. Every one of those is a persisted record that means something other
    than what it says, and reading it generously is how a round acquires an identity nobody chose.
    """
    if payload is None:
        return None
    try:
        return _decode_review_context(payload)
    except ValueError as exc:
        return DamagedReviewContext(reason=str(exc), payload=payload)


def _decode_review_context(payload: Any) -> ReviewRoundContext:
    """The strict reading. Raises ``ValueError`` with the one reason this payload is not a context."""
    if not isinstance(payload, dict):
        raise _ContextDamage(f"a persisted review context must be a JSON object, not {_json_type(payload)}")
    missing = [name for name in REVIEW_CONTEXT_FIELDS if name not in payload]
    if missing:
        raise _ContextDamage("the persisted review context is missing " + ", ".join(missing))
    unknown = sorted(name for name in payload if name not in REVIEW_CONTEXT_FIELDS)
    if unknown:
        raise _ContextDamage("the persisted review context carries unknown field " + ", ".join(unknown))
    return ReviewRoundContext(
        candidate_sha=_exact_sha(payload["candidate_sha"], "candidate_sha"),
        base_sha=_exact_sha(payload["base_sha"], "base_sha"),
        attempt_id=_attempt_id(payload["attempt_id"]),
        review_baseline=_review_baseline(payload["review_baseline"]),
        source=_source(payload["source"]),
    )


def _json_type(value: Any) -> str:
    """What a payload is, named the way the persisted document spells it."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, (list, tuple)):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return "an unsupported value"


def _text(value: Any, name: str) -> str:
    """A persisted string, as a string and nothing else. ``bool``/``int`` are not near-enough."""
    if not isinstance(value, str):
        raise _ContextDamage(f"review context {name} must be a string, not {_json_type(value)}")
    return value


def _exact_sha(value: Any, name: str) -> str:
    """A revision the writer already normalized. Case-folding here would accept two spellings."""
    text = _text(value, name)
    if not _EXACT_SHA_RE.fullmatch(text):
        raise _ContextDamage(f"review context {name} must be an exact lowercase 40-hex commit")
    return text


def _attempt_id(value: Any) -> str:
    text = _text(value, "attempt_id")
    if not text:
        raise _ContextDamage("review context attempt_id must name the attempt it was bound in")
    return text


def _review_baseline(value: Any) -> int:
    """A persisted comment count, validated for type, finiteness and range before it is believed."""
    if isinstance(value, bool):
        # True is an ``int`` to Python and a baseline of 1 to nobody.
        raise _ContextDamage("review context review_baseline must be a JSON integer, not a boolean")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _ContextDamage(
                f"review context review_baseline must be a finite integer; {value!r} is not finite"
            )
        raise _ContextDamage(
            f"review context review_baseline must be a JSON integer; {value!r} is a real number"
        )
    if not isinstance(value, int):
        raise _ContextDamage(
            f"review context review_baseline must be a JSON integer, not {_json_type(value)}"
        )
    if value < 0 or value > MAX_REVIEW_BASELINE:
        raise _ContextDamage(
            f"review context review_baseline must be between 0 and {MAX_REVIEW_BASELINE}"
        )
    return value


def _source(value: Any) -> str:
    text = _text(value, "source")
    if text not in REVIEW_CONTEXT_SOURCES:
        raise _ContextDamage("review context source must be one of " + ", ".join(REVIEW_CONTEXT_SOURCES))
    return text


# The one issuer of a validated identity. Held privately so the value below cannot be built by a
# path that skipped the authority: a gate or a merge asks for this type, and the type can only
# come from the single function that checked every durable source for it.
_POST_VERDICT_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class ValidatedReviewIdentity:
    """One review round's identity, after every durable source agreed on it.

    This is the value the verdict-driven half of the lifecycle runs on: the green park, the
    Assessment recovery, the observer release, the no-observer release, the mechanical readiness
    and drift checks, and the merge all take it as a required argument. It exists so that "we have
    not checked" and "we checked and it holds" are different *types* rather than two readings of
    the same optional field, and so that no path can reach a gate or a merge with a raw context, a
    receipt, or nothing at all.

    Only ``validate_post_verdict_identity`` can produce one; the seal below is the enforcement, not
    a formality, because the whole point is that the check cannot be skipped by writing the value
    out longhand at a call site.
    """

    candidate_sha: str
    base_sha: str
    attempt_id: str
    review_baseline: int
    verdict: str
    context_source: str
    seal: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.seal is not _POST_VERDICT_AUTHORITY:
            raise ValueError(
                "a validated review identity is issued only by validate_post_verdict_identity"
            )
        if not _EXACT_SHA_RE.fullmatch(self.candidate_sha) or not _EXACT_SHA_RE.fullmatch(self.base_sha):
            raise ValueError("a validated review identity names two exact lowercase 40-hex commits")
        if self.verdict not in {"green", "red"}:
            raise ValueError("a validated review identity carries the verdict it was validated for")

    @property
    def marker(self) -> str:
        """The Card marker this identity's verdict was published as."""
        return f"review:{self.verdict}"


def validate_post_verdict_identity(
    host: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    verdict: VerdictProjection,
) -> ValidatedReviewIdentity:
    """The single authority every verdict-driven path goes through. Validates; never establishes.

    Three durable things describe the round a standing verdict belongs to, and all three have to
    agree before anything acts on it:

    1. the persisted context, which is the round's identity — required, and required to name *this*
       attempt and *this* review round;
    2. the accepted verdict itself, whose structured header states the exact candidate and base it
       judged;
    3. the reviewer document this dispatcher recorded for the round, when it survives, which is
       what the reviewer was actually asked about.

    Any of them may disqualify the identity; none of them may supply it. A missing or damaged
    context is not an invitation to read the pair out of the document or the header — a verdict is
    already written by then, and a pair recovered to match it is an identity invented to fit an
    answer. Later mechanical receipts are not consulted at all: an assessment or release gate is
    evidence about its own stage over a base that may legitimately have moved since this round was
    opened.

    The comparison is always the whole pair. A candidate that matches while the base does not is
    still a different question, and it is exactly the shape that survived the previous, dispersed
    version of this check.
    """
    context = require_review_context(record)
    header = verdict.header
    if verdict.structure != "structured" or header is None:
        raise ReviewContextError(
            "the verdict standing on this review round carries no complete structured header, "
            "so there is nothing that says which candidate over which base it judged"
        )
    if not context.names_revisions(header.candidate_sha, header.base_sha):
        raise ReviewContextError(
            "the standing verdict judged "
            + _pair(header.candidate_sha, header.base_sha)
            + ", not this round's bound "
            + _pair(context.candidate_sha, context.base_sha)
        )
    recorded = _recorded_pair(host, task, record)
    if recorded is not None and not context.names_revisions(*recorded):
        raise ReviewContextError(
            "the recorded reviewer launch names "
            + _pair(*recorded)
            + ", not this round's bound "
            + _pair(context.candidate_sha, context.base_sha)
        )
    return ValidatedReviewIdentity(
        candidate_sha=context.candidate_sha,
        base_sha=context.base_sha,
        attempt_id=context.attempt_id,
        review_baseline=context.review_baseline,
        verdict=header.verdict,
        context_source=context.source,
        seal=_POST_VERDICT_AUTHORITY,
    )


def _pair(candidate_sha: str, base_sha: str) -> str:
    """One candidate/base pair, spelled the same way in every refusal an operator will read."""
    return f"candidate `{candidate_sha[:12]}` over base `{base_sha[:12]}`"


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

    This is the pre-verdict entry, and the only place a pair may be established. The recovery
    precedence is the whole contract:

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
                "the recorded reviewer launch names "
                + _pair(*recovered)
                + ", not this round's bound "
                + _pair(existing.candidate_sha, existing.base_sha)
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


def require_review_context(record: DispatcherRecord) -> ReviewRoundContext:
    """The bound context of the round the record is in, or a fail-closed refusal.

    Every read path goes through this rather than through the field, so "no context", "a context
    that could not be read back" and "a context from a round that has ended" are three specific
    answers at one site. Post-verdict callers do not use this directly: they go through
    ``validate_post_verdict_identity``, which starts here and then adds the two sources this alone
    cannot see.
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
