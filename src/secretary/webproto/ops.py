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
    RunNotFound,
    RuntimeUnavailable,
    ValidationRefused,
)
from secretary.webproto.runs import (
    DEFAULT_DEADLINE_SECONDS,
    HEADS_RELATIVE,
    RESULT_NAME,
    REVIEWER,
    WORKER,
    ProductRun,
    RunStore,
    RunStoreError,
)
from secretary.webproto.workspaces import provision, workspace_path
from triggered_agents.agents.pipeline.heads import HeadRegistryError, load_registry
from triggered_agents.runtime.codex_preflight import CodexPreflightError, preflight_codex_launch
from triggered_agents.runtime.head.command import HeadCommandError, render_head_command
from triggered_agents.runtime.head.operations import NudgePointer
from triggered_agents.runtime.head.run import HeadRun, HeadRunError, StopInitiator
from triggered_agents.runtime.head.runtime import HEAD_BUSY
from triggered_agents.runtime.head.spec import HeadSpec, HeadSpecError
from triggered_agents.runtime.head.task_ref import TaskRef
from triggered_agents.runtime.head_runtime_backends import build_head_runtime
from triggered_agents.runtime.head_runtimes import LOCAL_PTY_RUNTIME

SCHEMA_VERSION = 1

#: What the head is told about its own run, in its environment. A head reads the path it must write
#: its result to rather than being asked to invent one, so there is exactly one place a result can
#: appear and exactly one reader of it.
RESULT_ENV = "SECRETARY_RUN_RESULT"
RUN_ENV = "SECRETARY_RUN_ID"
ROLE_ENV = "SECRETARY_RUN_ROLE"
REF_ENV = "SECRETARY_RUN_REF"
WORKSPACE_ENV = "SECRETARY_RUN_WORKSPACE"

#: Who this product says ended a head it owns. A stop names its initiator, and this is ours.
INITIATOR = "secretary.webproto"

#: Where a Claude head's first-run answers live. The same file and the same environment override the
#: pipeline's own pane driver uses, so an operator has one place to look and one place to clear.
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))
STOP_RESULT_IN = "this run published its result, so the product that owns its process ended it"
STOP_DEADLINE = "this run passed its deadline without publishing a result"

#: The key an interactive composer reads as "send this", delivered on its own after the line it
#: sends. Two facts make it a second delivery rather than a suffix, and both were established
#: against a real Codex TUI:
#:
#: * the Enter key of a terminal is a **carriage return**. A TUI in raw mode reads a bare line feed
#:   — which is all the substrate appends — as a newline *inside* the message being composed, so a
#:   line delivered that way sits in the composer, gains one blank line per attempt, and the head
#:   never starts a turn;
#: * a composer treats one burst of bytes as a **paste**. Text and its carriage return written in a
#:   single payload are inserted together as text, so the return does not send anything either.
#:
#: So the line goes first and the return follows as its own payload, which is the shape a keyboard
#: has: the message, then Enter.
SUBMIT_KEY = "\r"

#: How long a bring-up waits for an interactive head to stop printing before it puts the task in
#: front of it, how much quiet counts as ready, and how often that is asked. A TUI that is drawing
#: its banner and starting its MCP servers is not ready for a line.
SETTLE_SECONDS = 90.0
SETTLE_QUIET_SECONDS = 4.0
SETTLE_POLL_SECONDS = 0.5


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
        """
        now = self._clock()
        report = self.report()
        data_dir = self.data_dir(report)
        store = self.store(data_dir)
        existing = self._existing(store, request_id)
        if existing is not None:
            return self._document(existing, now=now)

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
        )
        if not created:
            return self._document(run, now=now)
        with self._closing_a_failed_bring_up(run, store, now=now):
            workspace = provision(admission.repo, Path(run.workspace), base=admission.default_branch)
            document = self._worker_document(run, admission, instruction=instruction, base=workspace)
            run = self._raise(
                run,
                spec=spec,
                profile=profile_table,
                data_dir=data_dir,
                store=store,
                document=document,
                note=f"product run {run.run_id} for {run.ref}",
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
        """
        now = self._clock()
        report = self.report()
        data_dir = self.data_dir(report)
        store = self.store(data_dir)
        existing = self._existing(store, request_id)
        if existing is not None:
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
        )
        if not created:
            return self._review_document(run, store, now=now)
        with self._closing_a_failed_bring_up(run, store, now=now):
            document = self._review_prompt(run, worker, worker_state)
            run = self._raise(
                run,
                spec=spec,
                profile=profile_table,
                data_dir=data_dir,
                store=store,
                document=document,
                note=f"review of product run {worker.run_id} for {run.ref}",
            )
        run_events.publish_started(self._audit(data_dir), run)
        return self._review_document(run, store, now=now)

    def run_state(self, run_id: str) -> dict[str, Any]:
        """One run's state — and the one place a run's ending becomes durable.

        Reading is most of it, and the write is exactly one thing: the first observation of a
        terminal state settles the run and publishes its single `product_run.finished` event. That
        is not a second history — the event goes onto the card's own journal — and it is not a
        second answer either, because :meth:`secretary.webproto.runs.RunStore.settle` records an
        ending once and every later observer of the same ending publishes nothing.

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
        if state["value"] == "running":
            reason = self._reason_to_end(run, state, now=now)
            if reason:
                self._stop(run, data_dir, reason)
                state = run_state_reads.observe(run, now=now)
        if state["terminal"] and not run.settled:
            run, first = store.settle(run.run_id, state["value"], state["reason"], now=now)
            state = run_state_reads.observe(run, now=now)
            if first:
                run_events.publish_finished(self._audit(data_dir), run, state)
        return self._document(run, now=now, state=state)

    # -- the pieces the operations are made of ----------------------------------------------

    @contextlib.contextmanager
    def _closing_a_failed_bring_up(self, run: ProductRun, store: RunStore, *, now: float):
        """Close the run a failed bring-up opened, so a card is never left owned by nothing.

        The request id is claimed before the workspace is cut and the head is raised, which is what
        makes a retry idempotent. The cost of that ordering is a run record that exists while the
        bring-up is still going, and an unraised record that nothing ever settles would hold the
        card against every later run — a fence only a human could lift. So a bring-up that fails
        settles its own run, as `process_failed` and with the failure's own words, before the
        refusal reaches the caller.
        """
        try:
            yield
        except BaseException as exc:
            with contextlib.suppress(RunStoreError):
                store.settle(
                    run.run_id,
                    run_state_reads.PROCESS_FAILED,
                    f"this run's head could not be raised: {exc}",
                    now=now,
                )
            raise

    def _existing(self, store: RunStore, request_id: str) -> ProductRun | None:
        if not request_id:
            raise ValidationRefused("a product run operation names the request it is made under")
        try:
            return store.by_request(request_id)
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

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
            return store.claim(request_id, build)
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _raise(
        self,
        run: ProductRun,
        *,
        spec: HeadSpec,
        profile: dict[str, Any],
        data_dir: Path,
        store: RunStore,
        document: Path,
        note: str,
    ) -> ProductRun:
        """Bring one head up for this run, and record what the backend handed back."""
        Path(run.run_dir).mkdir(parents=True, exist_ok=True)
        self._preflight(run, spec=spec, profile=profile)
        pointer = NudgePointer.at_document(str(document), note)
        # An adapter that takes its prompt on its command line is launched with it; one that comes
        # up with an empty composer is pointed at the same document afterwards. The difference is
        # the adapter's, and `HeadSpec.prompt_after_start` is where the product already records it.
        prompt = None if spec.prompt_after_start else pointer.text
        try:
            rendered = render_head_command(profile, prompt=prompt, workspace=run.workspace, role="")
        except HeadCommandError as exc:
            raise ValidationRefused(f"this run's head command could not be rendered: {exc}") from None
        runtime = self._runtime(data_dir)
        receipt = runtime.start(
            spec,
            run.workspace,
            TaskRef.card(run.ref, document=str(document)),
            command=rendered.command,
            title=f"product-run:{run.run_id}",
            run_id=run.run_id,
            role=run.role,
            env=self._environment(run),
            subject=f"product-run:{run.run_id}",
        )
        if receipt.status == HEAD_BUSY:
            raise OwnerConflict(f"a head is already up for run {run.run_id}: {receipt.reason}")
        if not receipt.ok or receipt.run is None:
            raise RuntimeUnavailable(
                f"the product runtime could not raise this run's head: {receipt.reason or receipt.status}"
            )
        live = receipt.run
        if spec.prompt_after_start:
            live = self._point_at_the_task(runtime, live, run, pointer)
        raised = run.with_head(
            live.to_json(),
            head_pid=_pid_of(run),
            supervisor_pid=_supervisor_pid_of(run),
        )
        try:
            return store.save(raised)
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _point_at_the_task(self, runtime: Any, live: HeadRun, run: ProductRun, pointer) -> HeadRun:
        """Hand an interactive head its task, once it is actually ready to read one.

        The `start` verb can carry the pointer itself, and for this product it must not: an
        interactive TUI comes up over several seconds — it draws its banner, starts its MCP servers
        and only then owns its composer — and a line delivered into it before that lands in the
        composer *without being submitted*. The delivery is then perfectly true (every byte reached
        the terminal, and the substrate says so) and the head sits idle with its prompt unsent in
        front of it, which is exactly the failure this product runtime exists not to have: a run
        that reads as started and is not.

        So the head is raised bare and then driven the way a keyboard drives one, in four steps:

        1. **wait until it stops printing.** Read through the backend's own `observe`, off the
           supervisor's count of the bytes the head has produced — the only thing that moves while
           a TUI draws itself, because no turn is open yet and the journal records none;
        2. **deliver the line.** It lands in the composer, whole, and sends nothing;
        3. **wait until the backend says the head is idle again.** The substrate opens a turn of its
           own for every payload it carries and closes it when the head goes quiet, so a second
           delivery made straight away is refused `HEAD_BUSY` by a turn that is about the bytes
           rather than about the agent — and a refusal accepted as success is a prompt nobody sent;
        4. **deliver `SUBMIT_KEY` as its own payload.** One burst carrying the line and its
           carriage return is read as a paste and sends nothing, so Enter has to arrive by itself.

        `settle_seconds` bounds both waits. A head that never quietens still gets its line and its
        Enter, and the receipts are what say whether either landed: holding a run open with nothing
        in it is not the better failure.
        """
        subject = f"product-run:{run.run_id}"
        self._wait_until_quiet(runtime, live)
        delivered = runtime.deliver(live, pointer, subject=subject)
        if not getattr(delivered, "arrived", delivered.ok):
            raise RuntimeUnavailable(
                "this run's head came up and its task could not be put in front of it: "
                f"{delivered.reason or delivered.status}"
            )
        live = delivered.run or live
        self._wait_until_idle(runtime, live)
        sent = runtime.deliver(live, NudgePointer.line(SUBMIT_KEY), subject=f"{subject}:submit")
        if not getattr(sent, "arrived", sent.ok):
            raise RuntimeUnavailable(
                "this run's head was given its task and could not be told to send it: "
                f"{sent.reason or sent.status}"
            )
        return sent.run or live

    def _wait_until_quiet(self, runtime: Any, live: HeadRun) -> None:
        """Wait until the head has stopped printing, or until this bring-up's bound runs out."""
        deadline = time.monotonic() + self.settle_seconds
        printed = -1
        steady_since = time.monotonic()
        while time.monotonic() < deadline:
            receipt = runtime.observe(live)
            evidence = receipt.evidence if isinstance(receipt.evidence, dict) else {}
            current = evidence.get("output_bytes")
            current = current if isinstance(current, int) else -1
            if current != printed:
                printed, steady_since = current, time.monotonic()
            elif printed > 0 and time.monotonic() - steady_since >= self.settle_quiet_seconds:
                return
            time.sleep(self.settle_poll_seconds)

    def _wait_until_idle(self, runtime: Any, live: HeadRun) -> None:
        """Wait until the backend will take another payload for this head.

        `busy` is the backend's own answer and covers both halves of what would refuse the next
        delivery: the substrate's turn over the payload just carried, and the turn lease this
        runtime granted for it. A backend that cannot say (`busy` is `None`) is not waited on —
        an unknown is not a yes, and the delivery below reports its own refusal if there is one.
        """
        deadline = time.monotonic() + self.settle_seconds
        while time.monotonic() < deadline:
            if not runtime.observe(live).busy:
                return
            time.sleep(self.settle_poll_seconds)

    def _preflight(self, run: ProductRun, *, spec: HeadSpec, profile: dict[str, Any]) -> None:
        """Prepare the workspace for the head about to be raised into it, on its own runtime.

        The same two preparations the pipeline makes, reached directly rather than through the
        pane driver that also makes them: Codex' workspace trust is a hard precondition without
        which the TUI never reaches readiness, and Claude's trust and theme are best-effort.
        """
        if spec.adapter == "codex":
            try:
                preflight_codex_launch(
                    profile,
                    run.workspace,
                    HeadRun(
                        run_id=run.run_id,
                        spec=spec,
                        workspace=run.workspace,
                        task_ref=TaskRef.card(run.ref),
                        role=run.role,
                    ),
                )
            except (CodexPreflightError, HeadRunError) as exc:
                raise RuntimeUnavailable(
                    f"this run's Codex head could not be prepared for {run.workspace}: {exc}"
                ) from None
            return
        if spec.adapter == "claude":
            from triggered_agents.runtime import claude_env

            try:
                claude_env.ensure_trust(CLAUDE_JSON, run.workspace)
                claude_env.ensure_theme(CLAUDE_JSON)
            except claude_env.ClaudeConfigError:
                # Best-effort, exactly as it is on the pipeline's path: a head that lands on the
                # trust dialog is a delivery that does not arrive, and that is reported by the
                # receipt rather than guessed at here.
                pass

    def _environment(self, run: ProductRun) -> dict[str, str]:
        return {
            RESULT_ENV: run.result_path,
            RUN_ENV: run.run_id,
            ROLE_ENV: run.role,
            REF_ENV: run.ref,
            WORKSPACE_ENV: run.workspace,
            "SECRETARY_INSTANCE": str(self.instance),
        }

    def _reason_to_end(self, run: ProductRun, state: dict[str, Any], *, now: float) -> str:
        """Why the product should end the head this run holds, or nothing.

        The deadline is the earlier of the one the run was started with and the one this caller is
        configured with, so `--deadline-seconds` on a read shortens a run that is going nowhere and
        can never silently extend one past what its own start promised.
        """
        if state["result"]["present"]:
            return STOP_RESULT_IN
        deadlines = [moment for moment in (run.deadline_at, run.started_at + self.deadline_seconds) if moment]
        if deadlines and now >= min(deadlines):
            return STOP_DEADLINE
        return ""

    def _stop(self, run: ProductRun, data_dir: Path, reason: str) -> None:
        """End the head this run holds. The product owns the process, so the product ends it."""
        try:
            head_run = HeadRun.from_json(run.head_run)
        except (HeadRunError, ValueError, TypeError) as exc:
            raise RuntimeUnavailable(
                f"this run's head record could not be read back, so its process was not ended: {exc}"
            ) from None
        self._runtime(data_dir).stop(head_run, StopInitiator(INITIATOR, reason))

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


def _pid_of(run: ProductRun) -> int:
    return _identity_field(run, "pid")


def _supervisor_pid_of(run: ProductRun) -> int:
    """The supervisor's pid, from the file it writes into its own run directory."""
    try:
        return int(Path(run.run_dir, "supervisor.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _identity_field(run: ProductRun, name: str) -> int:
    """One integer out of the head's own launch-identity record, or zero when it has not landed.

    Diagnostic: the pid recorded on a run is what an operator greps for, and it is never what
    decides whether the run is alive — that is the classified heartbeat, in `run_state`.
    """
    try:
        record = json.loads(Path(run.pid_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    value = record.get(name) if isinstance(record, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
