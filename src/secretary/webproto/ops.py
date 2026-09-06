"""The mutation half of the transport-independent layer: three operations over one product run.

secretary-1561 gave this package the reads — what the installation is doing, what a card is doing,
what has happened to it. This is what produces the thing those reads show, without Orca and without
a transport:

* :meth:`OperationLayer.run_start` raises a real worker head for one card, in a workspace this
  product cut, under a supervisor this product owns;
* :meth:`OperationLayer.run_review` raises a real reviewer head *by the worker's result*: it
  establishes what the worker run ended as, refuses while that is still open, and hands the
  reviewer the worker's workspace and the worker's own result document;
* :meth:`OperationLayer.run_state` reads one run, and is the one place a run's ending is settled.

Every one of them returns a JSON-serialisable document against the published `web-run` schema, and
every refusal is a typed :class:`secretary.webproto.errors.ReadError` with a protocol code. There
is no status number anywhere in this package, and no web import in it — the same two properties the
read layer holds, checked the same way.

**What is owned here, and what is borrowed.** The workspace (`workspaces`), the run record
(`runs`), the run directory, the pid file, the journal and the result file are the product's. The
head's process is held by `LocalPtyHeadRuntime` — the backend that already exists — reached through
the product's one name-to-backend mapping, `head_runtime_backends.build_head_runtime`, with a
session factory that *raises*: a supervised head is not held by a session manager, and this says so
out loud instead of leaving a route to Orca open that nothing here is allowed to use. No pane is
created, listed, probed or reaped on any path below, because a supervisor leaves none; no Orca CLI
is spawned, no Orca RPC is spoken, and no repository inventory of Orca's is read. `secretary
run_start` and `run_state` are the two paths criterion 2 names, and `tests/test_web_run_protocol.py`
fails if either of them grows one.

**Which head runs is configuration, not code.** A profile id is an argument, it is resolved through
the head registry (`triggered_agents.agents.pipeline.heads`), and it must name the `local-pty`
runtime — a profile that names Orca's backend is refused here rather than quietly run under a
backend it does not declare. Nothing in this module knows the name of a model, an adapter or an
effort.

**One owner of a card.** Both start paths go through :func:`secretary.webproto.admission.admit`
before they build or spawn anything, and neither has a branch around it.

**The order between a process and the record of it is not decided here.** It is decided in
:mod:`secretary.webproto.lifecycle`, in one function, and this module calls it for every start,
every review and every close. What stays here is *policy* -- which card may run (`admission`),
which profile, when a run should be closed -- and what leaves is the order in which a run may be
raised, bound and ended without ever leaving a live head with no owner.

**A request id owns an operation, and the events of a run are never assumed published.** Two
properties that read as bookkeeping and are not. A request id is the idempotency key of one
operation made with one set of inputs, checked against the record it owns, so a review made under a
worker's id is a typed refusal rather than a review document about the worker's own run. And
raising a head and publishing its start are two durable writes, as are settling an ending and
publishing it: every path that hands back an existing or already-settled run republishes what that
run owes first (:meth:`OperationLayer._republish`), because criterion 6 is that a launch and an
outcome are visible through `task_events` and `task_snapshot`, not that they were once written.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary.config import InstanceReport, validate_instance
from secretary.tasks import KanboardClient, TaskAudit
from secretary.webproto import run_events, sources
from secretary.webproto import run_state as run_state_reads
from secretary.webproto.admission import Admission, admit
from secretary.webproto.errors import (
    InstallationUnavailable,
    OwnerConflict,
    ReadError,
    RunNotFound,
    RuntimeUnavailable,
    ValidationRefused,
)
from secretary.webproto.lifecycle import (
    CLAUDE_JSON,
    INITIATOR,
    SETTLE_POLL_SECONDS,
    SETTLE_QUIET_SECONDS,
    SETTLE_SECONDS,
    STOP_BRING_UP_FAILED,
    STOP_DEADLINE,
    STOP_ENDED,
    STOP_RESULT_IN,
    SUBMIT_KEY,
    RunLifecycle,
)
from secretary.webproto.runs import (
    DEFAULT_DEADLINE_SECONDS,
    HEADS_RELATIVE,
    RAISED,
    RAISING,
    RESULT_NAME,
    REVIEW_OPERATION,
    REVIEWER,
    SETTLED,
    START_OPERATION,
    WORKER,
    ProductRun,
    RequestMismatch,
    RunStore,
    RunStoreError,
    request_fingerprint,
)
from secretary.webproto.workspaces import provision, workspace_path
from triggered_agents.agents.pipeline.heads import HeadRegistryError, load_registry
from triggered_agents.runtime.head.command import HeadCommandError
from triggered_agents.runtime.head.spec import HeadSpec, HeadSpecError
from triggered_agents.runtime.head_runtime_backends import build_head_runtime
from triggered_agents.runtime.head_runtimes import LOCAL_PTY_RUNTIME

#: Re-exported so that the names an operator and a test already know keep resolving here, while the
#: transitions that use them live in one place. See :mod:`secretary.webproto.lifecycle`.
__all__ = [
    "CLAUDE_JSON",
    "INITIATOR",
    "OperationLayer",
    "STOP_BRING_UP_FAILED",
    "STOP_DEADLINE",
    "STOP_ENDED",
    "STOP_RESULT_IN",
    "SUBMIT_KEY",
    "no_session",
]

SCHEMA_VERSION = 1

#: What the head is told about its own run, in its environment. A head reads the path it must write
#: its result to rather than being asked to invent one, so there is exactly one place a result can
#: appear and exactly one reader of it.
RESULT_ENV = "SECRETARY_RUN_RESULT"
RUN_ENV = "SECRETARY_RUN_ID"
ROLE_ENV = "SECRETARY_RUN_ROLE"
REF_ENV = "SECRETARY_RUN_REF"
WORKSPACE_ENV = "SECRETARY_RUN_WORKSPACE"


def no_session() -> Any:
    """The session manager a product run never has.

    `build_head_runtime` is the product's one name-to-backend mapping and takes both backends'
    dependencies, because it is the one mapping for both. This layer only ever names the supervised
    one, and this is what says so out loud rather than leaving open a route to Orca that nothing
    here is allowed to use. It is a function and not a value so that naming the supervised backend
    never constructs the other one's dependency.
    """
    raise RuntimeUnavailable(
        "a product run's head is held by a supervisor of this product's own, not by a session manager"
    )


class OperationLayer:
    """One installation's product runtime, with no knowledge of who is asking.

    Construction does no I/O, exactly as `ReadLayer`'s does not: every operation resolves the
    instance, the store and the backend when it is called, so a long-lived transport holding one of
    these never acts on a configuration it read at start-up.

    `board_client`, `registry`, `spawn` and `clock` exist so a test — or a transport with its own
    connection policy — can supply those directly. None of them is a mode: the same code path runs
    with the live ones as with the fakes.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        registry_path: str | Path | None = None,
        registry: Any | None = None,
        runtime_factory: Callable[[Path], Any] | None = None,
        clock: Callable[[], float] = time.time,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        settle_seconds: float = SETTLE_SECONDS,
        settle_quiet_seconds: float = SETTLE_QUIET_SECONDS,
        settle_poll_seconds: float = SETTLE_POLL_SECONDS,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._board_client = board_client
        self._registry_path = Path(registry_path) if registry_path is not None else None
        self._registry = registry
        self._runtime_factory = runtime_factory
        self._clock = clock
        self.deadline_seconds = float(deadline_seconds)
        self.settle_seconds = float(settle_seconds)
        self.settle_quiet_seconds = float(settle_quiet_seconds)
        self.settle_poll_seconds = float(settle_poll_seconds)

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

    def store(self, data_dir: Path | None = None) -> RunStore:
        return RunStore(data_dir if data_dir is not None else self.data_dir())

    def _client(self) -> Any:
        return self._board_client or KanboardClient.for_instance(
            self.instance.parent if self.instance.is_file() else self.instance
        )

    def _registry_table(self) -> Any:
        if self._registry is not None:
            return self._registry
        try:
            return load_registry(self._registry_path)
        except HeadRegistryError as exc:
            raise ValidationRefused(f"the head registry could not be read: {exc}") from None

    def _profile(self, profile_id: str) -> tuple[HeadSpec, dict[str, Any]]:
        """One registry profile as a launchable spec, refused by name when it is not one.

        The `local-pty` check is here and not in the backend because the backend would happily hold
        any head: what would be wrong is running a head under a backend its own profile does not
        declare, so a profile naming Orca's backend is a configuration refusal rather than a silent
        substitution.
        """
        if not profile_id:
            raise ValidationRefused("a product run names the head profile it runs on")
        registry = self._registry_table()
        try:
            resolved = registry.resolve(profile_id)
            profile = dict(registry.profile(resolved))
            spec = HeadSpec.from_profile(resolved, profile)
        except (HeadRegistryError, HeadSpecError, HeadCommandError) as exc:
            raise ValidationRefused(f"head profile {profile_id!r} is not launchable: {exc}") from None
        if spec.runtime != LOCAL_PTY_RUNTIME:
            raise ValidationRefused(
                f"head profile {profile_id!r} declares the {spec.runtime!r} runtime; the product "
                f"runtime raises only {LOCAL_PTY_RUNTIME!r} heads, whose process, workspace, pid "
                "and logs this product owns"
            )
        return spec, profile

    def _runtime(self, data_dir: Path) -> Any:
        """This layer's backend, built through the product's one name-to-backend mapping.

        `runtime_factory` is the seam a test supplies its own backend through, and it is the only
        one: with none given this builds the supervised backend by name, with a session factory
        that raises. There is no branch here that could reach the other backend.
        """
        if self._runtime_factory is not None:
            return self._runtime_factory(data_dir)
        from secretary.dispatcher_watchdog import head_process_status

        return build_head_runtime(
            LOCAL_PTY_RUNTIME,
            session=no_session,
            local_pty_root=lambda: data_dir / HEADS_RELATIVE,
            head_process_status=head_process_status,
        )

    def _audit(self, data_dir: Path) -> TaskAudit:
        return TaskAudit(data_dir)

    # -- operations ------------------------------------------------------------------------

    def run_start(
        self,
        ref: str,
        *,
        request_id: str,
        profile: str,
        instruction: str = "",
    ) -> dict[str, Any]:
        """Raise a worker head for one card, or hand back the run this request id already owns.

        The order is the contract, and it is the order idempotency needs: the request id is claimed
        first, with the run id and every path already decided; the admission gate is passed before
        anything is built; and only then is a workspace cut and a head raised. A repeat of the same
        request id — a retried command, a client that reconnected — finds the record at the first
        step and returns it, so no second process and no second workspace can exist for it.

        "The same request id" means the same request: the id is claimed under this operation and a
        fingerprint of these inputs, so a repeat naming a different card, profile or instruction —
        or a different operation entirely — is refused rather than answered with somebody else's
        run. And the repeat republishes the run's `product_run.started` before returning it, so a
        launch whose publication failed once is not invisible forever.
        """
        now = self._clock()
        report = self.report()
        data_dir = self.data_dir(report)
        store = self.store(data_dir)
        fingerprint = request_fingerprint(
            START_OPERATION, {"ref": ref, "profile": profile, "instruction": instruction}
        )
        existing = self._existing(
            store, request_id, operation=START_OPERATION, fingerprint=fingerprint
        )
        if existing is not None:
            return self._document(existing, now=now, state=self._republish(data_dir, existing, now=now))

        admission = admit(
            ref,
            report=report,
            data_dir=data_dir,
            board=self._client(),
            store=store,
            production_state=data_dir / "dispatcher" / "production-state.json",
        )
        spec, profile_table = self._profile(profile)
        run, created = self._claim(
            store,
            request_id,
            ref=admission.ref,
            project=admission.project,
            role=WORKER,
            spec=spec,
            data_dir=data_dir,
            now=now,
            operation=START_OPERATION,
            fingerprint=fingerprint,
        )
        if not created:
            return self._document(run, now=now, state=self._republish(data_dir, run, now=now))
        lifecycle = self._lifecycle(data_dir, store)
        with self._closing_before_any_spawn(lifecycle, run, now=now) as prepared:
            workspace = provision(admission.repo, Path(run.workspace), base=admission.default_branch)
            document = self._worker_document(run, admission, instruction=instruction, base=workspace)
            run = prepared(
                lifecycle.advance(
                    run, RAISING, now=now, spec=spec, profile=profile_table, document=document
                )
            )
        run = lifecycle.advance(
            run,
            RAISED,
            now=now,
            spec=spec,
            profile=profile_table,
            document=document,
            note=f"product run {run.run_id} for {run.ref}",
            env=self._environment(run),
        )
        run_events.publish_started(self._audit(data_dir), run)
        return self._document(run, now=now)

    def run_review(
        self,
        *,
        request_id: str,
        profile: str,
        ref: str = "",
        worker_run_id: str = "",
    ) -> dict[str, Any]:
        """Raise a reviewer head for a worker run that has ended, by that run's own result.

        "By its result" is enforced rather than described: the worker run is settled through
        :meth:`run_state` first, and a worker that is still running is refused. Whatever it ended as
        — a published result, a non-zero exit, a process that died — is what the reviewer is handed,
        in the same workspace the worker worked in, so the review is of the work rather than of a
        report about it.

        The request id is claimed under *this* operation and this review's own inputs, which is what
        keeps that promise against a caller that repeats the worker's start id here: the repeat is a
        typed validation conflict rather than a `product_review` document carrying the worker's run,
        with no reviewer raised and nobody told.
        """
        now = self._clock()
        report = self.report()
        data_dir = self.data_dir(report)
        store = self.store(data_dir)
        fingerprint = request_fingerprint(
            REVIEW_OPERATION, {"ref": ref, "worker_run_id": worker_run_id, "profile": profile}
        )
        existing = self._existing(
            store, request_id, operation=REVIEW_OPERATION, fingerprint=fingerprint
        )
        if existing is not None:
            self._republish(data_dir, existing, now=now)
            return self._review_document(existing, store, now=now)

        worker = self._worker_run(store, ref=ref, worker_run_id=worker_run_id)
        worker_state = self.run_state(worker.run_id)
        if not worker_state["state"]["terminal"]:
            raise OwnerConflict(
                f"the worker run {worker.run_id} is {worker_state['state']['value']}: a review is "
                "raised by a worker's result, so it waits until that run has ended"
            )
        worker = store.get(worker.run_id) or worker

        admission = admit(
            worker.ref,
            report=report,
            data_dir=data_dir,
            board=self._client(),
            store=store,
            production_state=data_dir / "dispatcher" / "production-state.json",
        )
        spec, profile_table = self._profile(profile)
        run, created = self._claim(
            store,
            request_id,
            ref=admission.ref,
            project=admission.project,
            role=REVIEWER,
            spec=spec,
            data_dir=data_dir,
            now=now,
            parent_run_id=worker.run_id,
            workspace=worker.workspace,
            operation=REVIEW_OPERATION,
            fingerprint=fingerprint,
        )
        if not created:
            self._republish(data_dir, run, now=now)
            return self._review_document(run, store, now=now)
        lifecycle = self._lifecycle(data_dir, store)
        with self._closing_before_any_spawn(lifecycle, run, now=now) as prepared:
            document = self._review_prompt(run, worker, worker_state)
            run = prepared(
                lifecycle.advance(
                    run, RAISING, now=now, spec=spec, profile=profile_table, document=document
                )
            )
        run = lifecycle.advance(
            run,
            RAISED,
            now=now,
            spec=spec,
            profile=profile_table,
            document=document,
            note=f"review of product run {worker.run_id} for {run.ref}",
            env=self._environment(run),
        )
        run_events.publish_started(self._audit(data_dir), run)
        return self._review_document(run, store, now=now)

    def run_state(self, run_id: str) -> dict[str, Any]:
        """One run's state — and the one place a run's ending becomes durable.

        Reading is most of it, and the write is exactly one thing: the first observation of a
        terminal state settles the run, recording the state, the reason, the exit status and the
        result together, and its single `product_run.finished` event goes onto the card's own
        journal. That is not a second history, and not a second answer either:
        :meth:`secretary.webproto.runs.RunStore.settle` records an ending once, and the event is a
        pure function of that record, so every later observer that republishes it rebuilds the
        record the journal already holds. Republishing rather than publishing once is deliberate —
        the settle is durable before its publication is, and an ending lost to one journal failure
        would otherwise never become visible again.

        Two things this operation does besides observing, and both are what "the product owns the
        process" means. A run whose head has published its result is a run whose work is done, so
        the head holding it is ended here rather than left to sit in a terminal forever. A run past
        its deadline is ended for the opposite reason: nothing came, and a head nobody will ever
        read is not left running.
        """
        now = self._clock()
        data_dir = self.data_dir()
        store = self.store(data_dir)
        run = store.get(run_id)
        if run is None:
            raise RunNotFound(f"there is no product run {run_id!r} on this installation")
        state = run_state_reads.observe(run, now=now)
        closing = self._reason_to_close(run, state, now=now)
        if closing:
            run = self._lifecycle(data_dir, store).advance(run, SETTLED, now=now, reason=closing)
            state = run_state_reads.observe(run, now=now)
        if run.settled:
            # Not `if first`: an ending is settled once, and *publishing* it is a separate durable
            # write that may have failed after the settle. So every terminal read republishes,
            # which is free when the event is already on the journal and is the only way an ending
            # lost to one journal failure ever becomes visible again. See :meth:`_republish`.
            state = self._republish(data_dir, run, now=now, state=state) or state
        return self._document(run, now=now, state=state)

    # -- the pieces the operations are made of ----------------------------------------------

    def _lifecycle(self, data_dir: Path, store: RunStore) -> RunLifecycle:
        """This layer's one lifecycle, built per call exactly as the store and the backend are."""
        return RunLifecycle(
            store,
            self._runtime(data_dir),
            settle_seconds=self.settle_seconds,
            settle_quiet_seconds=self.settle_quiet_seconds,
            settle_poll_seconds=self.settle_poll_seconds,
        )

    @contextlib.contextmanager
    def _closing_before_any_spawn(self, lifecycle: RunLifecycle, run: ProductRun, *, now: float):
        """Close a run whose preparation failed, while it provably still holds no process.

        The request id is claimed before the workspace is cut, which is what makes a retry
        idempotent, and the cost of that ordering is a run record that exists while preparation is
        still going. An unraised record that nothing ever settles would hold the card against every
        later run -- a fence only a human could lift -- so a preparation that fails closes its own
        run before the refusal reaches the caller.

        The close goes through :meth:`RunLifecycle.advance` like every other close, and what it is
        handed is the newest record preparation produced: the block reports each phase it reaches
        through `prepared`, so a failure inside the write-ahead closes the record the write-ahead
        wrote rather than the stale one this block began with. Whether that close may settle is
        still the lifecycle's decision and not this block's -- a write-ahead that failed after its
        spawn window opened lands in `unresolved` from here exactly as it would from anywhere.
        """
        latest = [run]

        def prepared(current: ProductRun) -> ProductRun:
            latest[0] = current
            return current

        try:
            yield prepared
        except BaseException as exc:
            with contextlib.suppress(ReadError, RunStoreError):
                lifecycle.advance(
                    latest[0],
                    SETTLED,
                    now=now,
                    reason=STOP_BRING_UP_FAILED,
                    failure=f"this run's head could not be raised: {exc}",
                )
            raise

    def _existing(
        self, store: RunStore, request_id: str, *, operation: str, fingerprint: str
    ) -> ProductRun | None:
        """The run this exact request already owns, or nothing, or a typed refusal.

        The refusal is the point. A request id is the idempotency key of *one* operation made with
        *one* set of inputs, so the store is asked for the run under both, and a repeat that names
        a different operation or different inputs is a validation conflict rather than a document
        about a run that answers a different question. Without that, a review command that reused
        the worker's request id would be handed the worker's own run back, reported as a review,
        with no reviewer ever raised.
        """
        if not request_id:
            raise ValidationRefused("a product run operation names the request it is made under")
        try:
            return store.by_request(request_id, operation=operation, fingerprint=fingerprint)
        except RequestMismatch as exc:
            raise ValidationRefused(str(exc)) from None
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _republish(
        self,
        data_dir: Path,
        run: ProductRun,
        *,
        now: float,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Make sure this run's events are on the card's history, however often that is asked.

        Criterion 6 says the launch and the outcome are visible through the existing `task_events`
        and `task_snapshot`. Publishing them once, on the path that created them, only holds that
        while the journal never fails: a `TaskAudit` that is briefly unavailable *after* the head
        is up loses `product_run.started` forever, because the retry the idempotency contract
        invites finds the record and returns it without ever trying again. The same hole sits under
        `run_state`, whose settle is durable before its publication is.

        So publication is not a step of the creating path but a property every path restores: a
        recovered run republishes what it owes before it is returned. It costs nothing when the
        events are already there, because both are derived entirely from the run -- `occurred_at`
        included -- so a replay builds the byte-identical record `TaskAudit` already holds and
        recognises, and neither a second event nor a second history can come of it.
        """
        if not run.raised and not run.settled:
            return state
        observed = state if state is not None else run_state_reads.observe(run, now=now)
        audit = self._audit(data_dir)
        if run.raised:
            run_events.publish_started(audit, run)
        if run.settled:
            run_events.publish_finished(audit, run, observed)
        return observed

    def _claim(
        self,
        store: RunStore,
        request_id: str,
        *,
        ref: str,
        project: str,
        role: str,
        spec: HeadSpec,
        data_dir: Path,
        now: float,
        operation: str,
        fingerprint: str,
        parent_run_id: str = "",
        workspace: str = "",
    ) -> tuple[ProductRun, bool]:
        def build(run_id: str) -> ProductRun:
            run_dir = data_dir / HEADS_RELATIVE / run_id
            return ProductRun(
                run_id=run_id,
                request_id=request_id,
                ref=ref,
                project=project,
                role=role,
                profile=spec.profile_id,
                adapter=spec.adapter,
                runtime=spec.runtime,
                parent_run_id=parent_run_id,
                workspace=workspace or str(workspace_path(data_dir, run_id)),
                run_dir=str(run_dir),
                pid_file=str(run_dir / "head.pid"),
                journal_path=str(run_dir / run_state_reads.JOURNAL_NAME),
                log_path=str(run_dir / "supervisor.log"),
                result_path=str(run_dir / RESULT_NAME),
                started_at=now,
                deadline_at=now + self.deadline_seconds,
            )

        try:
            return store.claim(request_id, build, operation=operation, fingerprint=fingerprint)
        except RequestMismatch as exc:
            raise ValidationRefused(str(exc)) from None
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _environment(self, run: ProductRun) -> dict[str, str]:
        return {
            RESULT_ENV: run.result_path,
            RUN_ENV: run.run_id,
            ROLE_ENV: run.role,
            REF_ENV: run.ref,
            WORKSPACE_ENV: run.workspace,
            "SECRETARY_INSTANCE": str(self.instance),
        }

    def _reason_to_close(self, run: ProductRun, state: dict[str, Any], *, now: float) -> str:
        """Why this read should close the run, or nothing. The policy half of a close.

        Four answers, and the middle two are why this is a method rather than a condition:

        * a settled run is history and is never closed again;
        * an **unresolved** run is closed on every read, because that is what resolving it means:
          the stop is retried from the same durable record, and the run settles the moment the
          ending is confirmed. A card is therefore not fenced by an unresolved run for any longer
          than the head under it actually survives;
        * a running run is closed when its work is done or its time is up;
        * a run whose process evidence is already terminal is closed so that ending becomes
          durable, and one whose evidence says `unknown` or `source_unavailable` is not: a source
          that could not answer is not an ending.

        The deadline is the earlier of the one the run was started with and the one this caller is
        configured with, so `--deadline-seconds` on a read shortens a run that is going nowhere and
        can never silently extend one past what its own start promised.
        """
        if run.settled:
            return ""
        if run.unresolved:
            return STOP_ENDED
        if state["value"] == "running":
            if state["result"]["present"]:
                return STOP_RESULT_IN
            deadlines = [
                moment for moment in (run.deadline_at, run.started_at + self.deadline_seconds) if moment
            ]
            return STOP_DEADLINE if deadlines and now >= min(deadlines) else ""
        return STOP_ENDED if state["terminal"] else ""

    def _worker_run(self, store: RunStore, *, ref: str, worker_run_id: str) -> ProductRun:
        if worker_run_id:
            run = store.get(worker_run_id)
            if run is None:
                raise RunNotFound(f"there is no product run {worker_run_id!r} on this installation")
            if run.role != WORKER:
                raise ValidationRefused(f"run {worker_run_id!r} is a {run.role} run, not a worker run")
            return run
        if not ref:
            raise ValidationRefused("a review names the worker run it answers, or the card it is on")
        workers = [run for run in store.for_ref(ref) if run.role == WORKER]
        if not workers:
            raise RunNotFound(f"card {ref} carries no product worker run to review")
        return workers[-1]

    # -- the documents a head is pointed at ---------------------------------------------------

    def _worker_document(self, run: ProductRun, admission: Admission, *, instruction: str, base: str) -> Path:
        """The task document this run's worker is pointed at, written outside its workspace.

        Outside deliberately: a workspace's identity is its tracked diff, and a document written
        into it would be part of what the reviewer then reads as the worker's work.
        """
        card = admission.card
        body = "\n".join(
            [
                f"# {card.get('title') or run.ref}",
                "",
                f"Card: {run.ref}   Project: {run.project}   Run: {run.run_id}",
                f"Workspace: {run.workspace} (detached at {base or 'HEAD'})",
                "",
                "## What to do",
                "",
                str(card.get("description") or "").strip() or "(this card carries no description)",
                "",
                *([instruction.strip(), ""] if instruction.strip() else []),
                "## How this run ends",
                "",
                "Work only inside the workspace above. Commit nothing, push nothing and open no",
                "pull request: this run is a run, not a pipeline attempt.",
                "",
                f"When you are done, write your result as JSON to the path in ${RESULT_ENV}:",
                "",
                '    {"status": "done", "summary": "<one or two sentences>", "changed": ["<path>", ...]}',
                "",
                "Then stop working and wait. The product that owns this head reads that file, ends",
                "the process and records the run's outcome; you do not need to exit yourself.",
                "",
            ]
        )
        return self._write_document(run, "TASK.md", body)

    def _review_prompt(self, run: ProductRun, worker: ProductRun, worker_state: dict[str, Any]) -> Path:
        result = worker_state["state"]["result"]
        body = "\n".join(
            [
                f"# Review of product run {worker.run_id}",
                "",
                f"Card: {run.ref}   Project: {run.project}   Review run: {run.run_id}",
                f"Worker run: {worker.run_id} on {worker.profile}",
                f"Worker outcome: {worker_state['state']['value']} — {worker_state['state']['reason']}",
                f"Worker result file: {worker.result_path}",
                f"Worker journal: {worker.journal_path}",
                "",
                "## What to review",
                "",
                f"The worker's workspace is {run.workspace}. Read what it changed with",
                "`git status` and `git diff` there, read the worker's result file above, and judge",
                "whether the work the card asked for was actually done.",
                "",
                "## How this run ends",
                "",
                f"Write your verdict as JSON to the path in ${RESULT_ENV}:",
                "",
                '    {"verdict": "green"|"red", "summary": "<why>", "findings": ["<finding>", ...]}',
                "",
                "`green` means the work stands as it is. `red` means it does not, and the findings",
                "say what is wrong. Change nothing in the workspace; a review reads.",
                "",
                "Then stop working and wait. The product ends this head and records the verdict.",
                "",
                "## What the worker was asked to do",
                "",
                _read_text(Path(worker.run_dir) / "TASK.md"),
                "",
            ]
        )
        if not result["present"]:
            body += "\nThe worker published no result file. Say so in your verdict.\n"
        return self._write_document(run, "REVIEW.md", body)

    def _write_document(self, run: ProductRun, name: str, body: str) -> Path:
        path = Path(run.run_dir) / name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except OSError as exc:
            raise RuntimeUnavailable(f"this run's task document could not be written: {exc}") from None
        return path

    # -- documents -----------------------------------------------------------------------------

    def _document(
        self, run: ProductRun, *, now: float, state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        observed = state if state is not None else run_state_reads.observe(run, now=now)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "product_run",
            "observed_at": sources.isoformat(now),
            "run": _run_json(run),
            "state": observed,
            "reads": _reads(run),
        }

    def _review_document(self, run: ProductRun, store: RunStore, *, now: float) -> dict[str, Any]:
        worker = store.get(run.parent_run_id) if run.parent_run_id else None
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "product_review",
            "observed_at": sources.isoformat(now),
            "review": self._document(run, now=now),
            "worker": self._document(worker, now=now) if worker is not None else None,
        }


def _run_json(run: ProductRun) -> dict[str, Any]:
    payload = run.to_json()
    payload["started_at"] = sources.isoformat(run.started_at) if run.started_at else None
    payload["deadline_at"] = sources.isoformat(run.deadline_at) if run.deadline_at else None
    payload["settled_at"] = sources.isoformat(run.settled_at) if run.settled_at else None
    return payload


def _reads(run: ProductRun) -> dict[str, str]:
    """How this run is read back through the layer that already exists, spelled out for a client.

    Criterion 6 in one field: there is no run history and no run outcome store to point a reader at,
    so what a document points at is the card's own `task_events` and `task_snapshot`.
    """
    return {
        "task_events": f"secretary web-read events --ref {run.ref}",
        "task_snapshot": f"secretary web-read task --ref {run.ref}",
        "events_kind_started": run_events.STARTED,
        "events_kind_finished": run_events.FINISHED,
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "(the worker's task document could not be read)"
