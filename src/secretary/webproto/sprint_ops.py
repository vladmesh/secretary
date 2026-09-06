"""The operation that opens a sprint, and nothing else about what a sprint is.

secretary-1562 gave this package operations over a product run. This is the one operation over a
sprint: a client -- the web transport of the next card, `secretary sprint create` today, a Telegram
head later -- states a product, a goal, a definition of done, the issues the sprint serves, the
projects it reserves, the head that observes it and, optionally, the heads its cards run on, and a
sprint entity exists.

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
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary.config import InstanceReport, validate_instance
from secretary.sprint_observer import observer_choice
from secretary.sprints import SprintWriter
from secretary.tasks import KanboardClient, TaskError
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
#: branches on a value rather than on a sentence.
PENDING_REASON = "sprint_create_pending_repair"

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
        sprint_ref = str((created.get("sprint") or {}).get("ref") or "")
        if not sprint_ref:
            raise RuntimeUnavailable("the sprint writer created a sprint that carries no reference")
        self._remember(store, request_id, sprint_ref)
        return self._document(sprint_ref, request_id=request_id, claimed=claimed, now=now)

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

    def _remember(self, store: SprintRequestStore, request_id: str, reference: str) -> None:
        """Name the sprint this request produced, and refuse rather than hide a store that cannot.

        Deliberately not swallowed. A create whose reference could not be recorded is exactly the
        partial failure this layer promises to survive, and the promise is kept by the repeat
        resuming the writer's own transaction -- not by this call having succeeded. So the caller is
        told what happened, with the action that resolves it, instead of reading a success whose
        second half never landed.
        """
        try:
            store.record_reference(request_id, reference)
        except RunStoreError as exc:
            raise OperationPending(
                f"sprint {reference} was created and this layer could not record it under the "
                f"request that made it: {exc}",
                data=self._pending_action(request_id, reference=reference),
            ) from None

    def _refusal(self, exc: TaskError, *, request_id: str) -> Exception:
        """One `TaskError` from the sprint writer, as this layer's own typed failure.

        `audit_pending` is the one that is not a plain mapping, because it is not a plain refusal:
        it says the create is part-done and durably repairable, and the only safe move is to repeat
        *this* request, which resumes the row that may already exist. A new request id would open a
        second sprint beside the half-written one, so the action is data on the failure rather than
        a sentence a client has to read.
        """
        if exc.code == "audit_pending":
            return OperationPending(exc.message, data=self._pending_action(request_id))
        return _CODES.get(exc.code, RuntimeUnavailable)(exc.message)

    def _pending_action(self, request_id: str, *, reference: str = "") -> dict[str, Any]:
        return {
            "reason": PENDING_REASON,
            "action": {
                "operation": SPRINT_CREATE_OPERATION,
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
        return self._board_client or KanboardClient.for_instance(
            self.instance.parent if self.instance.is_file() else self.instance
        )


__all__ = ["PENDING_REASON", "SCHEMA_VERSION", "SPRINT_CREATE_ROLES", "SprintOperationLayer"]
