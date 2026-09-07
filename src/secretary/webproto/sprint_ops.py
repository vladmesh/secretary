"""The operations that open a sprint, comment on one and close one, and nothing else about what a
sprint is.

secretary-1562 gave this package operations over a product run. These are the operations over a
sprint. `sprint_create`: a client -- the web transport, `secretary sprint create` today, a Telegram
head later -- states a product, a goal, a definition of done, the issues the sprint serves, the
projects it reserves, the head that observes it and, optionally, the heads its cards run on, and a
sprint entity exists. `sprint_comment`: the one way a PO intervenes in a *running* sprint, by
commenting on the entity -- there is deliberately no path here to edit the sprint's cards.
`sprint_close`: the owner states why the sprint is ending, what became of every issue it declared
and every card it still holds, and the account of the outcome the close writes into
`state/knowledge`, and the sprint ends.

**A close is not a completed Definition of Done.** The answer says so in a field of its own, the
closeout the close writes says so in its first paragraph, and neither the operation nor the writer
has a spelling that means the goal was reached. A sprint may close with its contract only partly
satisfied -- that is the ordinary case, and it is why the decisions file exists. See
:data:`secretary.sprint_close.CLOSE_NOT_DONE`.

**A close needs no request index of this layer's own either**, and for a stronger reason than a
comment: `SprintWriter.close` stages the whole close under its request id, so a repeat resumes that
staged transaction, repeats no step whose derived id already carries a committed event, and refuses
one that states other decisions. An index here would be a second answer to that, and it could not
carry the one amendment a `close_conflict` retry is allowed to make. What this module adds is the
same two things it adds everywhere: a typed refusal instead of a `TaskError` with an exit status,
and a readable result -- which for a close is the read below, not a second description of what the
writer just did.

**A comment needs no request index of this layer's own.** `SprintWriter._write` already claims
`request_id` in the committed audit, and a repeat is answered from that claim *without the mutation
being called*: no board comment, no second event, and therefore nothing new for a delivery batch to
carry and no second observer wake. Building a second index beside it would be a second answer to a
question already answered. The one thing the audit does not do is refuse a repeat that reuses an id
over *different* inputs, and that -- and only that -- is added here, in the shape `sprint_create`
already refuses one.

**What a comment answers with is saved, not accepted.** The identifier is the audit event id, the
`saved` flag says whether this call wrote it, and the delivery status is
:meth:`~secretary.webproto.sprint_reads.SprintReadLayer.sprint_comment_delivery` -- a read over the
dispatcher's own cursors. None of the three says the observer read, accepted or took the comment
into account; that mechanism is deferred by the owner and tracked as
`secretary.webproto.sprint_reads.ACCEPTANCE_ISSUE`.

**Every rule stays where it already is.** `SprintWriter.create` owns all of them -- the product
must exist, at least one of the named issues must be an open issue of *that* product, every named
project must be registered, no open sprint may already hold one of those projects, the observer
must be a profile of this installation's head registry or the word `none`, and each executor pin
must be a profile too -- and this operation calls it rather than restating any of it. There is no
second admission gate here, no second audit and no second reservation index. What this module adds
is the two things a transport-independent layer needs and the writer does not have: a typed
refusal instead of a `TaskError` with an exit status, and a request id that owns the outcome.

**"Start" is not a verb here, and that is deliberate.** There is no operation that launches a
sprint observer, because there is no such action in this product: the production tick reconciles
open sprints against the observer records it holds and raises one head per sprint that lacks one
(`secretary.dispatcher_observer`). So opening a sprint *with* an observer is the whole of starting
it, and a scheduler of this layer's own would be a second thing racing the tick for the same head.
What this module does instead is tell the caller where the sprint is: the document it returns
carries the launch state :mod:`secretary.webproto.sprint_reads` reads off the dispatcher's own
production state, which right after a create says the entity is saved and no observer is up yet.

**A request id owns a sprint.** The same property `run_start` has, by the same mechanism: the id is
claimed in this layer's own request index (:mod:`secretary.webproto.sprint_requests`) before the
writer is called, and the reference of the sprint the create produced is recorded under it after.
A repeat of the same request answers from that record without touching the writer, so it can raise
no second entity and no second observer. The window between the two writes -- a sprint created and
not yet named here -- is exactly the partial failure criterion 4 is about, and it is closed by the
*same* id being handed down to `SprintWriter.create`, whose staged transaction resumes the row it
already began instead of opening another. Neither half is a distributed lock: what this defends
against is one operator's retry.

**And once a row exists, the answer is fixed whatever fails.** Any failure after
`SprintWriter.create` returns reaches the caller as an `OperationPending` carrying
`backend_unavailable`, the request id and the action "repeat this same request" -- whichever
primitive raised it, and with the durable fact stated before the cause. That rule is enforced over
the whole region rather than at any one primitive (:meth:`SprintOperationLayer._after_create`),
because two earlier rounds of this card fixed it at a primitive and watched it reappear at the next
one: the atomic writer's `RuntimeError`, then the lock's bare `OSError`. A region cannot grow a
third hole by acquiring a third primitive.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary.board.backend import SPRINT, board_client
from secretary.config import InstanceReport, validate_instance
from secretary.sprint_observer import observer_choice
from secretary.sprints import SprintWriter
from secretary.tasks import TaskAudit, TaskError, _digest
from secretary.webproto import sources
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.errors import (
    InstallationUnavailable,
    OperationPending,
    OwnerConflict,
    RuntimeUnavailable,
    TaskNotFound,
    ValidationRefused,
)
from secretary.webproto.runs import RequestMismatch, RunStoreError, request_fingerprint
from secretary.webproto.sprint_reads import SprintReadLayer
from secretary.webproto.sprint_requests import SPRINT_CREATE_OPERATION, SprintRequestStore

SCHEMA_VERSION = 1

#: The roles that may open a sprint, as `SprintWriter.create` already restricts them. Named here so
#: a client can offer the choice; the refusal for anything else is still the writer's own.
SPRINT_CREATE_ROLES = ("po", "steward")

#: The reason token an :class:`~secretary.webproto.errors.OperationPending` carries, so a client
#: branches on a value rather than on a sentence. One per operation, because the safe action is
#: about *that* operation's request id and a client that repeated the wrong one would be repeating
#: somebody else's half-finished write.
PENDING_REASON = "sprint_create_pending_repair"
COMMENT_PENDING_REASON = "sprint_comment_pending_repair"

#: The name a comment operation is known by on a pending action. Deliberately not a record in
#: :mod:`secretary.webproto.sprint_requests`: a comment needs no second request index, because the
#: audit's own committed/pending claim on `request_id` is already what makes a repeat idempotent.
SPRINT_COMMENT_OPERATION = "sprint_comment"

#: The name a close is known by on a pending action, and deliberately not a record in
#: :mod:`secretary.webproto.sprint_requests` either. `SprintWriter.close` already stages the whole
#: close under its request id and resumes it there; a second index here would be a second answer to
#: a question the staged transaction has already answered, and a second thing to keep in step with
#: the amendment a `close_conflict` retry is allowed to carry.
SPRINT_CLOSE_OPERATION = "sprint_close"
CLOSE_PENDING_REASON = "sprint_close_pending_repair"

#: The one role that may close a sprint, as `SprintWriter.close` already restricts it.
SPRINT_CLOSE_ROLES = ("po",)

#: The roles `SprintWriter.comment` admits, named here so a client can offer the choice. The refusal
#: for anything else is still the writer's own, and the operation restates none of it.
SPRINT_COMMENT_ROLES = ("po", "dispatcher", "worker", "reviewer", "steward", "retro")

#: The kind of audit event a sprint comment is, as `SprintWriter.comment` writes it. Read here only
#: to tell a repeat of *this* request from a request id that already owns some other sprint write.
COMMENT_EVENT_KIND = "commented"

#: How a `TaskError` from the sprint writer becomes a code of this layer. The writer's vocabulary is
#: the task protocol's, and every entry below is a mapping and never a re-decision: what was refused
#: and why is the writer's answer, and this only says which of *this* layer's four codes carries it.
#:
#: `sprint_conflict` and `resource_conflict` are `owner_conflict` for the reason that code exists:
#: the request was well formed and is refused on the state of the world -- another open sprint holds
#: the project, or the installation is at its open-sprint limit -- and the same request made after
#: that sprint closes is admitted.
_CODES: dict[str, Any] = {
    "validation": ValidationRefused,
    "role_forbidden": ValidationRefused,
    "not_found": TaskNotFound,
    "sprint_conflict": OwnerConflict,
    "resource_conflict": OwnerConflict,
    # A closed or stopped sprint refusing a write is the same kind of thing: the request is well
    # formed and refused on the state of the world. This layer only says which of its codes carries
    # the writer's answer -- what `SprintWriter._write` refuses, and when, is unchanged.
    "closed": OwnerConflict,
    # The two refusals a close makes on the state of the world, and the same reading: the request
    # is well formed, and it is refused because something outside it holds -- a card whose head is
    # still running, or an object somebody else moved while this close ran. Both are answered by
    # settling that thing and repeating the close, which is what `owner_conflict` means here.
    "live_work": OwnerConflict,
    "close_conflict": OwnerConflict,
    "backend_error": RuntimeUnavailable,
}


class SprintOperationLayer(ProtocolBoundary):
    """One installation's sprint operations, with no knowledge of who is asking.

    Construction does no I/O, exactly as the other two layers' does not: the instance, the board and
    the writer are resolved when an operation is called.

    `board_client` and `clock` are the seams a test -- or a transport with its own connection policy
    -- supplies directly. Neither is a mode: the same code path runs with the live board.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._board_client = board_client
        self._clock = clock

    # -- shared plumbing -------------------------------------------------------------------

    def report(self) -> InstanceReport:
        report = validate_instance(self.instance)
        if not report.ok or report.data_dir is None:
            raise InstallationUnavailable(
                "this instance config does not validate: "
                + "; ".join(str(error) for error in report.errors[:5])
            )
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    # -- operations ------------------------------------------------------------------------

    def sprint_create(
        self,
        *,
        request_id: str,
        actor: str,
        product: str,
        goal: str,
        issues: list[str] | None = None,
        projects: list[str] | None = None,
        observer: str = "",
        definition_of_done: str = "",
        repositories: list[str] | None = None,
        worker: str | None = None,
        reviewer: str | None = None,
        role: str = "po",
        reference: str = "",
    ) -> dict[str, Any]:
        """Open one sprint, or hand back the sprint this request id already opened.

        The order is the contract, and it is the order idempotency needs: the request id is claimed
        first, before anything on the board exists; the writer -- with every rule in it -- is called
        with that same id; and the reference it produced is recorded under the id afterwards. A
        repeat that finds a recorded reference answers from it and calls no writer at all, so it
        cannot create a second entity or a second observer. A repeat that finds a claim with no
        reference is the partial failure, and it resolves itself: the writer is called again with
        the same request id, resumes its own staged create and returns the sprint that already
        exists.

        `worker` and `reviewer` are optional in the full sense. `None` is the caller saying nothing
        about that role, it travels as `None` all the way into `SprintWriter._executor_intent`, and
        the row is written with no field for it -- which is what makes an unpinned role readable as
        unpinned rather than as pinned to the empty string or to a default nobody chose. There is
        deliberately no spelling that means "unpin": the empty string and `none` are refused below
        this layer rather than folded into absence.

        `observer` is the one word an operator must say: a profile id, or `none` for a sprint that
        runs without an observer. It is turned into the tagged value the entity stores by
        :func:`secretary.sprint_observer.observer_choice`, the same function the CLI uses, and every
        judgement about it -- unknown profile included -- is made by the writer.
        """
        now = self._clock()
        if not str(request_id or "").strip():
            raise ValidationRefused("a sprint operation names the request it is made under")
        report = self.report()
        data_dir = self.data_dir(report)
        store = SprintRequestStore(data_dir)
        issue_refs = list(issues or [])
        project_ids = list(projects or [])
        repository_roots = list(repositories or [])
        fingerprint = request_fingerprint(
            SPRINT_CREATE_OPERATION,
            {
                "role": role,
                "actor": actor,
                "product": product,
                "goal": goal,
                "definition_of_done": definition_of_done,
                "reference": reference,
                "observer": observer,
                # JSON rather than the values themselves, because `request_fingerprint` digests
                # strings: a list handed to it bare would fingerprint as the empty string, and two
                # requests differing only in their issues would look like one retry.
                "issues": json.dumps(issue_refs),
                "projects": json.dumps(project_ids),
                "repositories": json.dumps(repository_roots),
                # `null` and `"codex"` and `""` are three different requests, and the spelling has
                # to keep them apart here as carefully as the entity does.
                "worker": json.dumps(worker),
                "reviewer": json.dumps(reviewer),
            },
        )
        existing = self._existing(store, request_id, fingerprint=fingerprint)
        if existing is not None and existing.reference:
            return self._document(existing.reference, request_id=request_id, claimed=False, now=now)

        _record, claimed = self._claim(store, request_id, fingerprint=fingerprint, now=now)
        try:
            created = self._writer(report, data_dir).create(
                role=role,
                actor=actor,
                goal=goal,
                definition_of_done=definition_of_done,
                repositories=repository_roots,
                product=product,
                issues=issue_refs,
                projects=project_ids,
                reference=reference,
                request_id=request_id,
                observer=observer_choice(observer),
                worker=worker,
                reviewer=reviewer,
            )
        except TaskError as exc:
            raise self._refusal(exc, request_id=request_id) from None
        # From here on a sprint row exists on the board, and everything below is inside the one
        # region that says so however it fails. See :meth:`_after_create`.
        return self._after_create(store, created, request_id=request_id, claimed=claimed, now=now)

    def sprint_comment(
        self,
        *,
        request_id: str,
        actor: str,
        reference: str,
        body: str,
        role: str = "po",
    ) -> dict[str, Any]:
        """Put one comment on a sprint, and say where it got to without saying more than that.

        This is how a PO intervenes in a running sprint: a comment on the *entity*. There is
        deliberately no path here to edit the sprint's cards.

        **The identifier is the audit event id**, which is what makes it stable: it is minted once
        by `SprintWriter._write`, a repeat of the same request hands back the same one, and it is
        the identifier :meth:`~secretary.webproto.sprint_reads.SprintReadLayer.sprint_comment_delivery`
        takes back. It is not a board row number: a caller never has to know how to interpret it.

        **A repeat is idempotent by the audit's own claim, and by no second mechanism.**
        `SprintWriter._write` sees the committed event this request id already owns and returns it
        *without calling the mutation* -- so there is no second comment on the board, no second
        audit event, and therefore nothing new for a delivery batch to carry and no second observer
        wake. A second request index here would be a second answer to a question already answered;
        the one thing the audit does not do is refuse a repeat that reuses the id over *different*
        inputs, and that is what :meth:`_same_comment` adds, in exactly the shape `sprint_create`
        refuses one.

        `saved` says whether *this* call is the one that wrote it. False is the idempotency contract
        stated on the document rather than only in a test.

        `delivery` is the read below, embedded exactly as a create embeds the sprint: a caller that
        has just commented and a caller asking an hour later read the same document, and right after
        this call it honestly says the comment is saved and no batch carries it yet.
        """
        now = self._clock()
        if not str(request_id or "").strip():
            raise ValidationRefused("a sprint operation names the request it is made under")
        if not str(reference or "").strip():
            raise ValidationRefused("a sprint comment names the sprint it is made on")
        report = self.report()
        data_dir = self.data_dir(report)
        try:
            audit = TaskAudit(data_dir)
            owned = audit.committed_event(request_id) or audit.pending_event(request_id)
        except TaskError as exc:
            raise self._comment_refusal(exc, request_id=request_id) from None
        if owned is not None:
            self._same_comment(
                owned, role=role, actor=actor, reference=reference, body=body, request_id=request_id
            )
        try:
            written = self._writer(report, data_dir).comment(
                role=role, actor=actor, reference=reference, body=body, request_id=request_id
            )
        except TaskError as exc:
            raise self._comment_refusal(exc, request_id=request_id) from None
        comment_id = str(written.get("event_id") or "")
        if not comment_id:
            raise RuntimeUnavailable(
                "the sprint writer saved a comment that carries no durable identifier"
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint_comment",
            "observed_at": sources.isoformat(now),
            "request_id": request_id,
            "ref": reference,
            "comment_id": comment_id,
            # Whether this call saved it, or found it already saved. A repeat answers `false` and
            # writes nothing at all.
            "saved": owned is None,
            "delivery": self._reads().sprint_comment_delivery(reference, comment_id),
        }

    def sprint_close(
        self,
        *,
        request_id: str,
        actor: str,
        reference: str,
        reason: str,
        closeout: str,
        decisions: dict[str, list[dict[str, str]]] | None = None,
        role: str = "po",
    ) -> dict[str, Any]:
        """Close one sprint, and answer with what became of its work.

        The operation beside `sprint_create` and `sprint_comment`, and a client of
        `SprintWriter.close` in exactly the sense those two are clients of their writers: every rule
        about what a close *is* -- the decision each declared issue and each remaining card needs,
        the order of the terminal phase, the admission lock, the per-step request ids, `live_work`,
        `close_conflict`, the `already_closed`/`already_moved` confirmations and the `audit_pending`
        retry -- stays in `SprintWriter.close` and :mod:`secretary.sprint_close`. Nothing is
        re-decided here.

        **The closeout is required here and nowhere below.** The closing PO states what became of
        the work; the operation owns the document's path, its link to this sprint and the fact that
        it is written exactly once, and it invents none of its content. `SprintWriter.close` takes
        it as an option so that the callers that merely need a closed sprint -- recovery, tests, the
        dispatcher's own fixtures -- are not made to invent an account of one.

        **A repeat needs no request index of this layer's own.** The close is staged under its
        request id by the writer's own transaction: a repeat resumes that staged close, repeats no
        step it already committed, and is refused when it states other decisions. A second index
        here would be a second answer to that, and it could not carry the one amendment a
        `close_conflict` retry is allowed to make.

        **A close is not a completed Definition of Done**, and the document says so
        (:data:`secretary.sprint_close.CLOSE_NOT_DONE`) rather than leaving a reader to take
        `closed` for `done`.
        """
        from secretary.sprint_close import CLOSE_NOT_DONE

        now = self._clock()
        if not str(request_id or "").strip():
            raise ValidationRefused("a sprint operation names the request it is made under")
        if not str(reference or "").strip():
            raise ValidationRefused("a sprint close names the sprint it closes")
        if not str(reason or "").strip():
            raise ValidationRefused("a sprint close states why the owner is closing this sprint")
        if not str(closeout or "").strip():
            raise ValidationRefused(
                "a sprint close states what became of the work: pass the closeout this close writes "
                "into state/knowledge. It is the account of the outcome, not a claim that the "
                "Definition of Done was reached"
            )
        report = self.report()
        data_dir = self.data_dir(report)
        try:
            closed = self._writer(report, data_dir).close(
                role=role,
                actor=actor,
                reference=reference,
                decisions=decisions,
                request_id=request_id,
                reason=reason,
                closeout=closeout,
            )
        except TaskError as exc:
            raise self._close_refusal(exc, request_id=request_id) from None
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint_closed",
            "observed_at": sources.isoformat(now),
            "request_id": request_id,
            "ref": reference,
            "event_id": str(closed.get("event_id") or ""),
            # Said on the answer to the write as well as on the read below, because this is the
            # document a closing PO actually reads.
            "definition_of_done": {"satisfied": False, "reason": CLOSE_NOT_DONE},
            # The result, read back through the protocol exactly as a comment reads its delivery:
            # a caller that has just closed a sprint and one asking an hour later read the same
            # document, built from the sources that own each half of it.
            "result": self._reads().sprint_close_result(reference, str(closed.get("event_id") or "")),
        }

    # -- the pieces the operation is made of -------------------------------------------------

    def _existing(self, store: SprintRequestStore, request_id: str, *, fingerprint: str) -> Any:
        """The request this exact id already owns, or nothing, or a typed refusal.

        The refusal is the point, and it is `run_start`'s: a request id is the idempotency key of
        one operation made with one set of inputs, so a repeat naming a different product, goal or
        pin is a validation conflict rather than a document about somebody else's sprint.
        """
        try:
            return store.by_request(
                request_id, operation=SPRINT_CREATE_OPERATION, fingerprint=fingerprint
            )
        except RequestMismatch as exc:
            raise ValidationRefused(str(exc)) from None
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _claim(
        self, store: SprintRequestStore, request_id: str, *, fingerprint: str, now: float
    ) -> tuple[Any, bool]:
        try:
            return store.claim(
                request_id, operation=SPRINT_CREATE_OPERATION, fingerprint=fingerprint, now=now
            )
        except RequestMismatch as exc:
            raise ValidationRefused(str(exc)) from None
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _after_create(
        self,
        store: SprintRequestStore,
        created: dict[str, Any],
        *,
        request_id: str,
        claimed: bool,
        now: float,
    ) -> dict[str, Any]:
        """Everything this operation does once a sprint row exists, and the one answer it fails with.

        This region is the enforcement point of one invariant, and it is deliberately a *region*
        rather than a primitive: **any** failure after `SprintWriter.create` has returned reaches
        the caller as an :class:`~secretary.webproto.errors.OperationPending` carrying
        `backend_unavailable`, the request id and the action "repeat this same request" -- whatever
        raised it. Two rounds of this card were spent chasing that answer from one primitive to the
        next, because the rule was written about the primitive: first the atomic writer's
        `RuntimeError` (fixed at :mod:`secretary.webproto.store_io`, which stays), then the lock's
        bare `OSError` out of `file_lock`'s `mkdir`, `open` and `flock`. A third primitive added to
        this region tomorrow would have been a third hole. It is not, because nothing here is
        allowed to leave except through the one `except` below.

        What is caught is `Exception` and not a list of vocabularies, for exactly that reason: a
        list is the thing that has to be kept in step, and the failure this region must not have is
        one nobody thought to list. The cause is not lost -- it is chained (`raise ... from exc`),
        so a traceback still names the primitive and a defect of this layer is still visible where
        it happened; what changes is that the caller is *first* told the durable fact.

        And that ordering is the second half of the rule. The fact that the entity exists outranks
        the reason the step failed: the message opens with the sprint that was created and the safe
        move, and only then says what went wrong, because a caller that reads the cause first and
        acts on it opens a second sprint.
        """
        reference = ""
        try:
            reference = str((created.get("sprint") or {}).get("ref") or "")
            if not reference:
                raise RuntimeUnavailable("the sprint writer created a sprint that carries no reference")
            store.record_reference(request_id, reference)
            return self._document(reference, request_id=request_id, claimed=claimed, now=now)
        except Exception as exc:
            raise OperationPending(
                self._pending_message(reference, exc),
                data=self._pending_action(request_id, reference=reference),
            ) from exc

    @staticmethod
    def _pending_message(reference: str, cause: Exception) -> str:
        """The durable fact first, the cause after it. Both, in that order, always."""
        subject = f"sprint {reference}" if reference else "this sprint"
        return (
            f"{subject} was created and the request that made it did not finish; repeat the same "
            f"request id to pick it up rather than opening a second sprint. "
            f"The step that failed was {type(cause).__name__}: {cause}"
        )

    def _same_comment(
        self,
        owned: dict[str, Any],
        *,
        role: str,
        actor: str,
        reference: str,
        body: str,
        request_id: str,
    ) -> None:
        """Refuse a repeat that reuses this id over different inputs, rather than answering it.

        The refusal `sprint_create` already makes, over the record that already exists rather than
        over a second index of this layer's own: a request id is the idempotency key of *one*
        request, so a repeat naming another sprint, another role or another body is a `validation`
        conflict and never a document about the comment somebody else wrote. The comparison is made
        against the audit event the id owns -- its kind, its sprint, its actor and the body digest
        `SprintWriter.comment` puts on it -- because that record is what the writer would otherwise
        hand straight back.
        """
        actor_of = owned.get("actor") if isinstance(owned.get("actor"), dict) else {}
        payload = owned.get("payload") if isinstance(owned.get("payload"), dict) else {}
        same = (
            str(owned.get("kind") or "") == COMMENT_EVENT_KIND
            and str(owned.get("ref") or "") == reference
            and str(actor_of.get("role") or "") == role
            and str(actor_of.get("id") or "") == actor
            and str(payload.get("body_sha256") or "") == _digest(body)
        )
        if not same:
            raise ValidationRefused(
                f"request id {request_id!r} already owns a sprint write made with different inputs; "
                "a repeat is a retry of the same request, not a new one"
            )

    def _comment_refusal(self, exc: TaskError, *, request_id: str) -> Exception:
        """One `TaskError` from `SprintWriter.comment`, as this layer's own typed failure.

        The same mapping the create uses -- it is the writer's vocabulary either way -- with the
        pending action naming *this* operation and *this* request id, because that is the id whose
        repeat resumes the write that did not finish.
        """
        return self._refusal(
            exc,
            request_id=request_id,
            operation=SPRINT_COMMENT_OPERATION,
            reason=COMMENT_PENDING_REASON,
        )

    def _close_refusal(self, exc: TaskError, *, request_id: str) -> Exception:
        """One `TaskError` from `SprintWriter.close`, as this layer's own typed failure.

        The same mapping every sprint write uses. `audit_pending` is the one that is not a plain
        refusal: a close that has performed a step is never thrown away, so the answer is a pending
        action naming *this* request id -- the id whose repeat resumes the staged close, keeps its
        plan and repeats no step it already committed. A new id would open a second close beside a
        half-finished one.
        """
        return self._refusal(
            exc,
            request_id=request_id,
            operation=SPRINT_CLOSE_OPERATION,
            reason=CLOSE_PENDING_REASON,
        )

    def _refusal(
        self,
        exc: TaskError,
        *,
        request_id: str,
        operation: str = SPRINT_CREATE_OPERATION,
        reason: str = PENDING_REASON,
    ) -> Exception:
        """One `TaskError` from the sprint writer, as this layer's own typed failure.

        `audit_pending` is the one that is not a plain mapping, because it is not a plain refusal:
        it says the create is part-done and durably repairable, and the only safe move is to repeat
        *this* request, which resumes the row that may already exist. A new request id would open a
        second sprint beside the half-written one, so the action is data on the failure rather than
        a sentence a client has to read.
        """
        if exc.code == "audit_pending":
            return OperationPending(
                exc.message,
                data=self._pending_action(request_id, operation=operation, reason=reason),
            )
        return _CODES.get(exc.code, RuntimeUnavailable)(exc.message)

    def _pending_action(
        self,
        request_id: str,
        *,
        reference: str = "",
        operation: str = SPRINT_CREATE_OPERATION,
        reason: str = PENDING_REASON,
    ) -> dict[str, Any]:
        return {
            "reason": reason,
            "action": {
                "operation": operation,
                "repeat_request": True,
                "request_id": request_id,
                # Present only when this layer knows which sprint the half-finished request holds.
                "reference": reference or None,
            },
        }

    def _writer(self, report: InstanceReport, data_dir: Path) -> SprintWriter:
        """The sprint writer of this installation, built per call as every other source is.

        The budget thresholds come off the instance config this operation already validated, rather
        than from a second read of the same file: two reads could disagree across an edit, and the
        sprint would then be opened with thresholds nothing else on this installation uses.
        """
        thresholds = report.instance.get("sprint_budget") if isinstance(report.instance, dict) else None
        return SprintWriter(
            self._client(),
            data_dir=data_dir,
            thresholds=thresholds if isinstance(thresholds, dict) else None,
            instance=self.instance,
        )

    def _reads(self) -> SprintReadLayer:
        """The read layer this operation answers through, built with this layer's own seams."""
        return SprintReadLayer(
            self.instance,
            data_dir=self._data_dir,
            board_client=self._board_client,
            clock=self._clock,
        )

    def _document(
        self, reference: str, *, request_id: str, claimed: bool, now: float
    ) -> dict[str, Any]:
        """What a create answers with: the request, and the sprint as the read layer reads it.

        The sprint is not described a second time here. A client that has just opened one and a
        client watching one an hour later read the same document, which is why the observer's launch
        state is on the answer to a create at all: right after this call it says the entity is saved
        and the production tick has raised nothing for it yet, and that is the honest state rather
        than a claim that something was started.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "sprint_created",
            "observed_at": sources.isoformat(now),
            "request_id": request_id,
            # Whether *this* call claimed the request. A repeat answers `false` and creates nothing,
            # which is the idempotency contract stated on the document rather than only in a test.
            "created": claimed,
            "sprint": self._reads().sprint_state(reference),
        }

    def _client(self) -> Any:
        """The sprint board of this installation, named through the switch (board/backend.py)."""
        return self._board_client or board_client(
            self.instance.parent if self.instance.is_file() else self.instance, serves=(SPRINT,)
        )


__all__ = [
    "CLOSE_PENDING_REASON",
    "COMMENT_PENDING_REASON",
    "PENDING_REASON",
    "SCHEMA_VERSION",
    "SPRINT_CLOSE_OPERATION",
    "SPRINT_CLOSE_ROLES",
    "SPRINT_COMMENT_OPERATION",
    "SPRINT_COMMENT_ROLES",
    "SPRINT_CREATE_ROLES",
    "SprintOperationLayer",
]
