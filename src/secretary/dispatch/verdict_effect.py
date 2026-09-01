"""The one replayable executor for every verdict-driven Assessment move and merge.

A substantive reviewer verdict earns the card exactly one durable effect: it is parked in
Assessment for an observer to answer, or its branch is merged. Both are irreversible from the
outside — a board move other roles read, a commit published on the base branch — and both used to
be reached by more than one route. A verdict tick performed one; a recovery tick performed
another; a release decision performed a third. Each route re-established a different subset of the
preconditions, and the subsets were the defect: the no-observer release recovered from a crash by
calling the merge with the identity re-checked and nothing else, so a checkout that had advanced
since the intent was written, over a base no receipt covered, could still be landed.

So there is one executor, and it is the only production owner of those two effects. Its typed
input is ``VerdictEffectIntent``: what this card is owed and how far the effect got. It is intent,
never evidence. It does not claim the round's identity still holds, that the checkout is still on
the reviewed candidate, that the base has not moved, that a gate was executed or that a receipt is
valid. Every one of those is re-established here, in order, immediately before the effect, on
every invocation — first execution, lost checkpoint, dispatcher-record recovery, observer release
and no-observer release alike.

The ordered chain is:

1. select the verdict standing on this round — an occurrence, not a marker scraped from prose;
2. obtain the sealed ``ValidatedReviewIdentity`` from the one post-verdict authority;
3. resolve the checkout as it is now and the base branch as it is now;
4. refuse a candidate that drifted off the reviewed one, and a base whose history no longer
   contains the base this round was judged over;
5. execute the gate this stage requires;
6. accept and persist a fresh dispatcher-owned exact-SHA receipt, and refuse one that names a
   different candidate or a base that predates the round's;
7. and only then move the card to Assessment, or merge it, as the intent permits.

Steps 3 to 6 are the mechanical half, and which stage requires them is the one place stage policy
lives (``required_gate_stage``). A merge always requires them, at the release stage. The Assessment
park of a green verdict requires them at the assessment stage, which is where the fresh receipt the
observer reads comes from. The Assessment park of a red verdict requires no broad gate: it lands a
card in front of a person, merges nothing, and inventing a gate for it would spend a CI run to
decide a question nobody asked. That is a stage policy, evaluated identically on the first
execution and on every replay — not a recovery arm that finishes less than the tick before it did.

Nothing here decides what a verdict meant, what effect it earned or which round it belongs to.
Those answers come from the durable intent and from ``review_context``'s single authority; this
module only refuses to perform an effect until every one of them holds again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from secretary.board.events import VerdictProjection, project_verdict, render_marker_comment
from secretary.board.models import Event
from secretary.dispatch.review_context import (
    ReviewContextError,
    ValidatedReviewIdentity,
    validate_post_verdict_identity,
)
from secretary.dispatcher_gate import GateResult, GateTransportError
from secretary.dispatcher_gate_receipt import GateReceipt
from secretary.dispatcher_helpers import scrub_host_output
from secretary.dispatcher_state import attempt_request_id
from secretary.dispatcher_types import STOPPED_BY_REVIEW_VERDICT, HostError
from secretary.dispatcher_watchdog import reset_wait
from secretary.dispatcher_worker_lifecycle import (
    PARK_EFFECT_ASSESSMENT,
    PARK_EFFECT_RELEASE,
    PARK_EFFECTS,
    WorkerContinuation,
)

if TYPE_CHECKING:
    from secretary.dispatcher_state import DispatcherRecord


@dataclass(frozen=True, slots=True)
class VerdictEffectIntent:
    """What one standing verdict is owed, and how far its effect got. Intent, never evidence.

    Everything here is either the effect's kind, the identity that makes replaying it idempotent,
    or the one progress fact a replay cannot re-derive. Deliberately absent: the round's
    candidate/base identity, the checkout, the base, the drift answer, the gate result, the receipt
    and any notion of merge readiness. A crash may sit between this record and the effect, and none
    of those survive one — they are re-established by the executor on every invocation instead.
    """

    effect: str
    report_baseline: int
    move_reason: str
    verdict_outcome: str
    decision: str = ""
    merge_published: bool = False

    def __post_init__(self) -> None:
        if self.effect not in PARK_EFFECTS:
            raise ValueError(f"unknown verdict effect {self.effect}")

    @property
    def merges(self) -> bool:
        """Whether this intent's effect is the merge rather than the Assessment move."""
        return self.effect == PARK_EFFECT_RELEASE

    @classmethod
    def from_continuation(cls, continuation: WorkerContinuation) -> VerdictEffectIntent:
        """Read back the intent a previous tick opened, exactly as it was written.

        A record written before the effect was named carries none, and an unnamed effect is the
        Assessment move: that was the only effect a park had when those records were released, and
        it stays their supported behaviour.
        """
        return cls(
            effect=continuation.park_effect or PARK_EFFECT_ASSESSMENT,
            report_baseline=int(continuation.report_baseline),
            move_reason=continuation.move_reason,
            verdict_outcome=continuation.verdict_outcome,
            decision=continuation.decision,
            merge_published=bool(continuation.merge_published),
        )


# The one issuer of the value below. Held privately for the same reason the review identity's seal
# is: an effect asks for this type, and the type can only come from the chain that established it.
_EFFECT_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class EffectPreconditions:
    """Everything this invocation re-established, immediately before the effect it guards.

    The effects take this as a required argument, and only ``_establish_preconditions`` can build
    one. That is the enforcement, not a formality: it is what makes "the chain ran in this tick"
    and "an intent survived a crash" different types rather than two readings of the same record,
    so no entry and no recovery can reach the board move or the merge with less.
    """

    intent: VerdictEffectIntent
    identity: ValidatedReviewIdentity
    checkout_sha: str
    base_sha: str
    gate_stage: str
    receipt: GateReceipt | None
    seal: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.seal is not _EFFECT_AUTHORITY:
            raise ValueError("effect preconditions are established only by the verdict executor")


def required_gate_stage(intent: VerdictEffectIntent, identity: ValidatedReviewIdentity) -> str:
    """The mechanical stage this effect must execute, or "" when the stage requires no broad gate.

    The whole of the stage policy, in one place, read from the durable intent and the validated
    verdict so that it answers the same thing on the first execution and on every replay.

    * A merge is always the release stage. Whether that stage can produce a valid exact-SHA receipt
      is the project's gate mode to answer, not this function's; what is fixed here is that a merge
      never happens without asking.
    * Parking a green verdict in Assessment is the assessment stage: the receipt it mints is the
      fresh evidence the observer reads beside the report and the verdict.
    * Parking a red verdict requires no broad gate. It merges nothing and lands the card in front
      of a person whose next move is rework, reslice or a release that runs the release stage
      itself. Running a broad gate for it would spend a check to answer a question nobody asked.
    """
    if intent.merges:
        return "release"
    return "assessment" if identity.verdict == "green" else ""


@dataclass(frozen=True, slots=True)
class _MechanicalOutcomes:
    """How a non-green mechanical answer is answered, on the side of the seam the card is on.

    Both sides run the same chain; what differs is the disposition each already had. A card still
    in Validate has a worker round to hand back to, so a red gate or a drifted checkout bounces it
    there. A card standing in Assessment has spent its round: the same answers block it, naming the
    decision they refused. Read from where the card is, so a replay answers the way the tick that
    opened the intent would have.
    """

    step: str
    blocks: bool
    transport_action: str
    reason_prefix: str

    @classmethod
    def of(cls, task: dict[str, Any]) -> _MechanicalOutcomes:
        if str(task.get("state") or "") == "assessment":
            return cls(
                "assessment", True, "release-gate-transport-blocked", "Observer decision: release. "
            )
        return cls("review", False, "merge-gate-transport-blocked", "")


def standing_verdict(runtime: Any, task: dict[str, Any], record: DispatcherRecord) -> VerdictProjection | None:
    """The verdict occurrence standing on this review round, or ``None`` when there is none.

    Selection only: which occurrence this round is being answered by, read from the durable record
    rather than from the comment prose. An occurrence counts when the marker comment that published
    it is on the card after this round's baseline and its body is exactly what the event renders — a
    staged event whose comment never landed is not a published verdict, and a comment nothing
    committed is not one either.

    Deliberately no identity is consulted here. A verdict whose header names another pair, or that
    carries no readable header at all, is still the verdict standing on this round: it is handed to
    the post-verdict authority, which refuses it by name and lands the card on the board. Filtering
    it out here instead would make a contradicted verdict indistinguishable from a round nobody has
    answered yet, and the card would wait forever on an answer it already has.
    """
    comments = (task.get("comments") or [])[record.review_baseline :]
    for raw in reversed(runtime.audit.events(task["ref"])):
        if raw.get("kind") != "card.verdict":
            continue
        try:
            event = Event.from_record(raw)
            projection = project_verdict(event)
            rendered = render_marker_comment(event)
        except (TypeError, ValueError):
            continue
        marker = event.data.get("marker")
        if any(
            comment.get("marker") == marker and comment.get("body") == rendered for comment in comments
        ):
            return projection
    return None


def post_verdict_identity(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    verdict: VerdictProjection,
    *,
    step: str,
) -> ValidatedReviewIdentity | dict[str, Any]:
    """This round's validated identity, or the tick's fail-closed outcome for not having one.

    The one door between a standing verdict and everything that acts on one. Every caller — this
    executor, and the red transition the executor does not own — reaches the authority through here
    and either receives the typed identity or returns the Blocked outcome unchanged, so no path can
    be written that carries on with a weaker value.
    """
    try:
        return validate_post_verdict_identity(runtime.host, task, record, verdict)
    except ReviewContextError as exc:
        return runtime.block_review_context(
            task, record, records, payload, attempt_id, step=step, reason=scrub_host_output(str(exc))
        )


def run_verdict_effect(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    intent: VerdictEffectIntent,
) -> dict[str, Any]:
    """Perform one verdict effect, having re-established every precondition it depends on.

    The only production owner of the board's Assessment move and of the merge. Every entry arrives
    here with an intent and nothing else: a freshly described one from the verdict tick or the
    release decision, or one read back off the record by a recovery. Neither is treated differently,
    because the difference between them is exactly the thing an intent cannot record.
    """
    ref = task["ref"]
    outcomes = _MechanicalOutcomes.of(task)
    if intent.merge_published:
        # The irreversible half of this effect is already on the base branch. Re-running the chain
        # could only refuse something that has happened, and refusing it would leave the card
        # short of Done over a merge nothing can take back. What is left is this effect's own
        # bookkeeping, and it is idempotent.
        return _finish_merge(runtime, task, record, records, payload, attempt_id, intent)
    established = _establish_preconditions(
        runtime, task, record, records, payload, attempt_id, intent, outcomes
    )
    if not isinstance(established, EffectPreconditions):
        return established
    if not intent.merges and record.owns_head("review"):
        # The checkout must be quiet while the card waits, so the reviewer's pane goes here — after
        # the gate that may still bounce this round, and before the intent that commits to the
        # move. Its pane address is all that is forgotten: the round's context outlives it, and is
        # what the release decision will still be checked against. A red park and a recovery both
        # arrive with the pane already closed, and neither ends a reviewer a second time.
        unconfirmed = runtime._end_review_pane_confirmed(
            record,
            records,
            payload,
            ref,
            step=outcomes.step,
            attempt_id=attempt_id,
            initiator=STOPPED_BY_REVIEW_VERDICT,
        )
        if unconfirmed is not None:
            return unconfirmed
    # The intent is on disk, with the reason the card is moving and the effect it is owed, before
    # anything observable happens. Re-opening the one this record already carries is the same write.
    record.worker_continuation.begin_park(
        "review",
        intent.report_baseline,
        intent.move_reason,
        intent.verdict_outcome,
        intent.effect,
        intent.decision,
    )
    records[ref] = record
    runtime.save_records(payload, records)
    if intent.merges:
        return _publish_merge(runtime, task, record, records, payload, attempt_id, established)
    return _move_to_assessment(runtime, task, record, records, payload, attempt_id, established)


def _establish_preconditions(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    intent: VerdictEffectIntent,
    outcomes: _MechanicalOutcomes,
) -> EffectPreconditions | dict[str, Any]:
    """The ordered chain, run in full immediately before the effect. The only issuer of the seal.

    Returns the sealed preconditions, or the tick's outcome for the first one that did not hold.
    Nothing is carried in from the caller and nothing is read out of the intent except which effect
    is being asked for: a value validated by an earlier tick, or by an earlier step of this one, is
    not evidence on the far side of a crash.
    """
    ref = task["ref"]
    standing = standing_verdict(runtime, task, record)
    if standing is None:
        # The effect was opened on a verdict, so this is not a round waiting for an answer: the
        # occurrence it was opened over is no longer readable on the card.
        return runtime.block_review_context(
            task,
            record,
            records,
            payload,
            attempt_id,
            step=outcomes.step,
            reason="no accepted reviewer verdict stands on the review round this effect was opened for",
        )
    identity = post_verdict_identity(
        runtime, task, record, records, payload, attempt_id, standing, step=outcomes.step
    )
    if not isinstance(identity, ValidatedReviewIdentity):
        return identity
    stage = required_gate_stage(intent, identity)
    if not stage:
        # This stage requires no broad gate and lands nothing on the base branch. The identity is
        # the whole precondition, and it has just been established.
        return EffectPreconditions(
            intent=intent,
            identity=identity,
            checkout_sha="",
            base_sha="",
            gate_stage="",
            receipt=None,
            seal=_EFFECT_AUTHORITY,
        )
    checkout_sha = str(runtime.host.head_commit(record) or "")
    base_sha = str(runtime.host.review_base_commit(task, record) or "")
    drift = _drift(runtime, task, record, identity, checkout_sha, base_sha)
    if drift:
        # The gate was never asked here; the bounce clears the record's gate state itself.
        return _refuse(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            outcomes,
            kind="drift",
            detail=drift,
            result=None,
        )
    try:
        result = runtime.host.gate_check(task, record)
    except GateTransportError as exc:
        # A stage that could not ask the gate is not a stage that was refused.
        retry = runtime._gate_transport_retry(
            task, record, records, payload, attempt_id, exc, step=outcomes.step
        )
        if retry is not None:
            return retry
        return runtime._block_gate_transport(
            task,
            record,
            records,
            payload,
            attempt_id,
            step=outcomes.step,
            action=outcomes.transport_action,
            prefix=outcomes.reason_prefix,
        )
    except HostError as exc:
        runtime._gate_answered(ref, record, records, payload)
        return _refuse(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            outcomes,
            kind="failed",
            detail=scrub_host_output(str(exc)),
            result=None,
        )
    runtime._gate_answered(ref, record, records, payload)
    if result.status != "green":
        if result.status == "pending":
            return runtime._gate_pending(
                task,
                record,
                records,
                payload,
                attempt_id,
                result,
                step=outcomes.step,
                action="merge-gate-pending",
            )
        return _refuse(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            outcomes,
            kind="red",
            detail="",
            result=result,
        )
    blocked = runtime._accept_green_gate(
        task, record, records, payload, attempt_id, result, stage=stage
    )
    if blocked is not None:
        return blocked
    receipt = GateReceipt.accept(record.gate_attestation, current_sha=checkout_sha)
    mismatch = _receipt_mismatch(runtime, record, identity, receipt, checkout_sha)
    if mismatch:
        return runtime._block_merge_path(
            task,
            record,
            records,
            payload,
            attempt_id,
            action=f"{stage}-gate-receipt-blocked",
            reason=f"{outcomes.reason_prefix}{stage} gate receipt {mismatch}",
            step=outcomes.step,
            outcome=f"{stage} gate receipt does not name this round",
        )
    return EffectPreconditions(
        intent=intent,
        identity=identity,
        checkout_sha=checkout_sha,
        base_sha=base_sha,
        gate_stage=stage,
        receipt=receipt,
        seal=_EFFECT_AUTHORITY,
    )


def _drift(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    identity: ValidatedReviewIdentity,
    checkout_sha: str,
    base_sha: str,
) -> str:
    """Has the code this verdict describes moved? The operator message, or "" when it has not.

    Two halves, and both are the whole pair's question. The checkout must still hold the candidate
    the reviewer was pointed at, or the effect would land work nobody read. And the base branch must
    still contain the base the round was judged over: a base that advanced is the ordinary case and
    keeps it as an ancestor, while a base that was rewritten leaves the reviewed delta describing a
    history that no longer exists.

    Neither half bounces on an answer that could not be read: an unreadable workspace is the host's
    failure to report, not evidence about the code.
    """
    if (
        checkout_sha
        and checkout_sha != identity.candidate_sha
        and not runtime.host.is_instance_publish_recovery(task, record, identity.candidate_sha, checkout_sha)
    ):
        return (
            f"The review was given for commit `{identity.candidate_sha[:12]}` while the working copy "
            f"is now on `{checkout_sha[:12]}`: the verdict describes a different state of the code. "
            f"The card is back in In progress; rework it and report again."
        )
    if (
        base_sha
        and base_sha != identity.base_sha
        and not runtime.host.base_ancestry_intact(record, identity.base_sha, base_sha)
    ):
        return (
            f"The review was given over base `{identity.base_sha[:12]}`, which the base branch at "
            f"`{base_sha[:12]}` no longer descends from: the history the verdict read has been "
            f"replaced, so its delta no longer exists. The card is back in In progress; rework it "
            f"and report again."
        )
    return ""


def _receipt_mismatch(
    runtime: Any,
    record: DispatcherRecord,
    identity: ValidatedReviewIdentity,
    receipt: GateReceipt | None,
    checkout_sha: str,
) -> str:
    """Why this stage's fresh receipt is not about this round's candidate and base, or "".

    An explicitly non-attesting gate produces no receipt and that is its documented answer, already
    settled by the accepting policy above; there is nothing here to compare. A receipt that does
    exist has to name the candidate this chain just validated, and a base the round's own base is
    still an ancestor of — a receipt executed over an older base is evidence about a history this
    verdict was never given.
    """
    if receipt is None:
        return ""
    if receipt.validated_sha != identity.candidate_sha or receipt.validated_sha != checkout_sha:
        return (
            f"names candidate `{receipt.validated_sha[:12]}`, not the reviewed "
            f"`{identity.candidate_sha[:12]}`"
        )
    if receipt.base_sha != identity.base_sha and not runtime.host.base_ancestry_intact(
        record, identity.base_sha, receipt.base_sha
    ):
        return (
            f"names base `{receipt.base_sha[:12]}`, which does not descend from the reviewed base "
            f"`{identity.base_sha[:12]}`"
        )
    return ""


def _refuse(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    outcomes: _MechanicalOutcomes,
    *,
    kind: str,
    detail: str,
    result: GateResult | None,
) -> dict[str, Any]:
    """One non-green mechanical answer, disposed of the way the card's own side of the seam does.

    Not a new vocabulary: these are the outcomes each side already had, kept word for word so an
    operator reading a bounced or blocked card sees what that card has always said.
    """
    if not outcomes.blocks:
        if kind == "drift":
            # The gate was never asked here; the bounce clears the record's gate state itself.
            return runtime._gate_red_to_worker(
                task, record, records, payload, attempt_id, GateResult("red", detail), phase="review-freeze"
            )
        if result is not None:
            return runtime._gate_red_to_worker(
                task, record, records, payload, attempt_id, result, phase="merge-gate"
            )
        return runtime._block_merge_path(
            task,
            record,
            records,
            payload,
            attempt_id,
            action="merge-gate-blocked",
            reason=f"merge gate failed: {detail}",
            step=outcomes.step,
            outcome="merge gate failed",
        )
    summary = {
        "drift": f"the release cannot land: {detail}",
        "failed": f"the merge gate could not be read: {detail}",
        "red": "the mechanical gate is no longer green for the checkout this release was decided on",
    }[kind]
    return runtime._block_merge_path(
        task,
        record,
        records,
        payload,
        attempt_id,
        action=f"release-{kind}-blocked",
        reason=f"{outcomes.reason_prefix}{summary}",
        step=outcomes.step,
        outcome=f"release {kind}",
    )


def _move_to_assessment(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    preconditions: EffectPreconditions,
) -> dict[str, Any]:
    """Park the card in Assessment. The only production write of that board transition.

    Keyed on the baseline the intent was opened against, so the tick that already moved the card and
    the tick recovering from a crash before that move issue the same request and it moves once.
    """
    ref = task["ref"]
    continuation = record.worker_continuation
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="assessment",
        reason=preconditions.intent.move_reason,
        request_id=attempt_request_id(
            record.attempt_id or attempt_id,
            "review-assessment",
            ref,
            str(preconditions.intent.report_baseline),
        ),
    )
    continuation.confirm_park()
    record.state = "assessment"
    reset_wait(record, "review")
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "review",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "to": "assessment",
        "verdict": continuation.verdict_outcome,
    }


def _publish_merge(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    preconditions: EffectPreconditions,
) -> dict[str, Any]:
    """Merge the reviewed branch. The only production call of the merge effect.

    The sealed preconditions are a required argument rather than something read here, and that is
    what makes this function unreachable without them: there is no spelling of this call that lands
    a candidate whose round nobody could identify, whose checkout had moved, or whose release stage
    produced no valid exact-SHA receipt.
    """
    ref = task["ref"]
    identity = preconditions.identity
    try:
        runtime.host.complete_green(task, record)
    except HostError as exc:
        # A rejected merge must land the card in Blocked rather than escape the tick: an escaping
        # error leaves the verdict standing and every later tick retries the merge.
        return runtime._block_merge_path(
            task,
            record,
            records,
            payload,
            attempt_id,
            action="merge-blocked",
            reason=(
                f"merge of reviewed candidate `{identity.candidate_sha[:12]}` failed: "
                f"{scrub_host_output(str(exc))}"
            ),
            step=_MechanicalOutcomes.of(task).step,
            outcome="merge failed",
        )
    # The one progress fact this intent records, written between the publication and the Done move
    # it still owes. A replay that finds it finishes that bookkeeping instead of merging again.
    record.worker_continuation.note_merge_published()
    records[ref] = record
    runtime.save_records(payload, records)
    return _finish_merge(runtime, task, record, records, payload, attempt_id, preconditions.intent)


def _finish_merge(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    intent: VerdictEffectIntent,
) -> dict[str, Any]:
    """Tear the round down and move the merged card to Done. Idempotent, and it merges nothing."""
    ref = task["ref"]
    step = _MechanicalOutcomes.of(task).step
    runtime.host.teardown(record)
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="done",
        reason=intent.move_reason,
        decision=intent.decision,
        request_id=attempt_request_id(record.attempt_id or attempt_id, "review-green", ref),
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "to": "done"}


__all__ = [
    "PARK_EFFECT_ASSESSMENT",
    "PARK_EFFECT_RELEASE",
    "EffectPreconditions",
    "VerdictEffectIntent",
    "post_verdict_identity",
    "required_gate_stage",
    "run_verdict_effect",
    "standing_verdict",
]
