"""The one place a product run changes phase, and the order that makes a run safe to own.

Three rounds of this card produced three defects of the same shape, and the shape is the point:

* a head was spawned and its `product_run.started` was lost, because the spawn and the event were
  two writes and only one of them was retried;
* an ending was settled and its `product_run.finished` was lost, for the same reason;
* a head was spawned and `RunStore.save` failed, so a **live head was left with no owner** while
  the record settled as `process_failed` and admission let a second run onto the same card.

Each of those was repaired where it was found, and each repair moved the seam one step along. What
they have in common is that the order between "a process may now exist" and "the durable record
says so" was decided in three different places -- `_raise`, the bring-up's failure handler, and
`run_state` -- and each of them was free to decide it differently. This module is that decision,
made once.

**The phases.** `claimed → raising → raised → settled`, with `unresolved` as the honest branch out
of the last step, and their meanings are in :mod:`secretary.webproto.runs`.

**The order, and why it is this order.**

1. **Write-ahead.** A durable record by which a head can be *found and stopped* exists **before** a
   spawn can put a process into the world. Not a handle in memory -- the process that spawns is not
   always the process that must clean up -- but what lies on disk: the run directory and the pid
   path, which `local-pty` derives anyway, plus enough of a head description to address one. So
   the order is `claim → durable raising → spawn → bind the handle`, and a failure to bind after a
   successful spawn no longer orphans anything: the record already points at it.

2. **Ownership is recovered from disk, not from the spawn's return value.**
   `LocalPtyHeadRuntime.stop` addresses a head through `_address`, which derives the run directory
   from `root/run_id` and the pid file from the run's own `pid_file`, and it confirms the ending
   from the launch identity on that path. It never consults anything this process remembers. That
   is what makes the write-ahead record sufficient on its own, and it is checked by a test that
   stops a head using only a record rebuilt from the store.

3. **The truth about a possibly-live process outranks closing the record.** A cleanup that was not
   confirmed may not settle as a terminal outcome. `_close` therefore ends the head *first* and
   settles only on a confirmed ending; when the ending cannot be confirmed the run goes to
   `unresolved`, which:

   * reads through :mod:`secretary.webproto.run_state` as `unknown` -- one of the five words the
     read layer already has, never `finished` or `process_failed`, and never terminal;
   * keeps the run **unsettled**, which is exactly what makes `admission.admit`'s existing sixth
     condition refuse a second run on that card. No new rule and no new register: the gate already
     refuses a card that carries an unsettled run, and the previous code got past it only by
     settling a run it had no right to settle.

   An unresolved run is not a dead end. Every later `_close` -- a `run_state` of that run -- retries
   the stop from the same disk record, and settles the moment the ending is confirmed.

**What "one place" means as a check.** Every path that can put a process into the world, and every
path that can close a run, calls :meth:`RunLifecycle.advance` and nothing else. Within
`secretary.webproto` the backend's `start` and `stop` verbs and `RunStore.settle` are called from
this module and from no other, and `tests/test_web_run_protocol.py` fails if that stops being true.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from secretary.webproto import run_state as run_state_reads
from secretary.webproto.errors import (
    OwnerConflict,
    ReadError,
    RuntimeUnavailable,
    ValidationRefused,
)
from secretary.webproto.runs import (
    CLAIMED,
    RAISED,
    RAISING,
    SETTLED,
    UNRESOLVED,
    ProductRun,
    RunStore,
    RunStoreError,
)
from triggered_agents.runtime.codex_preflight import CodexPreflightError, preflight_codex_launch
from triggered_agents.runtime.head.command import HeadCommandError, render_head_command
from triggered_agents.runtime.head.operations import NudgePointer
from triggered_agents.runtime.head.run import HeadRun, HeadRunError, StopInitiator
from triggered_agents.runtime.head.runtime import HEAD_BUSY, HEAD_OK
from triggered_agents.runtime.head.spec import HeadSpec
from triggered_agents.runtime.head.task_ref import TaskRef

#: Who this product says ended a head it owns. A stop names its initiator, and this is ours.
INITIATOR = "secretary.webproto"

#: Where a Claude head's first-run answers live. The same file and the same environment override the
#: pipeline's own pane driver uses, so an operator has one place to look and one place to clear.
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))

#: The key an interactive composer reads as "send this", delivered on its own after the line it
#: sends. Two facts make it a second delivery rather than a suffix, and both were established
#: against a real Codex TUI:
#:
#: * the Enter key of a terminal is a **carriage return**. A TUI in raw mode reads a bare line feed
#:   -- which is all the substrate appends -- as a newline *inside* the message being composed, so a
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

#: Why the product ends a head it owns. Each is the `reason` on the stop's initiator, so an
#: operator reading a supervisor journal sees which of them happened.
STOP_RESULT_IN = "this run published its result, so the product that owns its process ended it"
STOP_DEADLINE = "this run passed its deadline without publishing a result"
STOP_ENDED = "this run reached a terminal state, so the product ended the head it owned"
STOP_BRING_UP_FAILED = "this run's bring-up failed, so the product ended whatever it had raised"

#: The transitions this lifecycle allows. Anything else is a defect of a caller, not a refusal to
#: report to a user, so it is raised as a runtime failure with the pair that was asked for.
ALLOWED: dict[str, frozenset[str]] = {
    CLAIMED: frozenset({RAISING, SETTLED}),
    RAISING: frozenset({RAISED, SETTLED, UNRESOLVED}),
    RAISED: frozenset({SETTLED, UNRESOLVED}),
    UNRESOLVED: frozenset({SETTLED, UNRESOLVED}),
    SETTLED: frozenset({SETTLED}),
}


class RunLifecycle:
    """One installation's product runs, moving between phases and nowhere else.

    Holds the store and the backend, and no policy: *when* a run should be closed is the operation
    layer's decision (a published result, a passed deadline, a failed bring-up), and *how* a run is
    closed without lying about it is this.
    """

    def __init__(
        self,
        store: RunStore,
        runtime: Any,
        *,
        instance_env: dict[str, str] | None = None,
        settle_seconds: float = SETTLE_SECONDS,
        settle_quiet_seconds: float = SETTLE_QUIET_SECONDS,
        settle_poll_seconds: float = SETTLE_POLL_SECONDS,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.instance_env = dict(instance_env or {})
        self.settle_seconds = float(settle_seconds)
        self.settle_quiet_seconds = float(settle_quiet_seconds)
        self.settle_poll_seconds = float(settle_poll_seconds)

    # -- the one function ----------------------------------------------------------------------

    def advance(self, run: ProductRun, to: str, *, now: float, **evidence: Any) -> ProductRun:
        """Move one run to its next phase, durably, in the order the phases require.

        Every path in this package that can put a process into the world or close a run comes
        through here, and the three targets are the whole of it:

        ``raising``  prepare the head's workspace and write the record that can address it. After
                     this returns, and only after, may a spawn be attempted;
        ``raised``   attempt that spawn, point the head at its task, and bind what came back. Any
                     failure from here is compensated by a close, not by a raised exception alone;
        ``settled``  close the run: end whatever head it may hold, confirm that ending from disk,
                     and settle once. An ending that cannot be confirmed lands in ``unresolved``
                     instead of settling, so the answer this returns is not always the one asked
                     for -- refusing to lie about a possibly-live process is the point of it.
        """
        if to not in ALLOWED.get(run.phase, frozenset()):
            raise RuntimeUnavailable(
                f"a product run does not move from {run.phase!r} to {to!r}; this run is "
                f"{run.run_id}"
            )
        if to == RAISING:
            return self._prepare(run, **evidence)
        if to == RAISED:
            return self._raise_the_head(run, now=now, **evidence)
        return self._close(run, now=now, **evidence)

    # -- claimed -> raising --------------------------------------------------------------------

    def _prepare(self, run: ProductRun, *, spec: HeadSpec, profile: dict[str, Any], document: Path) -> ProductRun:
        """The write-ahead. After this the record can address and stop a head; before it, nothing can.

        The head description written here is the same one the backend would build for itself:
        `LocalPtyHeadRuntime.start` constructs a `HeadRun` out of the run id, the spec, the
        workspace, the task ref and the role, and every address it later derives -- run directory,
        socket, journal, pid file -- comes from the run id and this `pid_file`. So a record written
        *before* the spawn addresses the very head that spawn produces.
        """
        try:
            Path(run.run_dir).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeUnavailable(f"this run's directory could not be created: {exc}") from None
        self._preflight(run, spec=spec, profile=profile)
        ahead = HeadRun(
            run_id=run.run_id,
            spec=spec,
            workspace=run.workspace,
            task_ref=self._task_ref(run, document),
            role=run.role,
            pid_file=run.pid_file,
        )
        return self._save(run.with_head(ahead.to_json(), phase=RAISING))

    # -- raising -> raised ---------------------------------------------------------------------

    def _raise_the_head(
        self,
        run: ProductRun,
        *,
        now: float,
        spec: HeadSpec,
        profile: dict[str, Any],
        document: Path,
        note: str,
        env: dict[str, str],
    ) -> ProductRun:
        """Bring one head up for this run, and close the run if anything about that goes wrong.

        The compensation is not a nicety here. Every exit from this method other than a bound, saved
        run goes through :meth:`_close`, and what it carries there is `spawned` -- whether the
        backend returned a **successful receipt** for this start. That is the line, and it is drawn
        where the backend draws it: a start the backend itself reports as failed left no process
        (`LocalPtyHeadRuntime` turns a spawn failure into a receipt rather than a half-raised head),
        while everything after a successful receipt -- the prompt delivery, the submit, the save --
        happens over a process that is provably there. A close carrying `spawned` may not settle
        until that head has been ended and confirmed gone. Raising the original failure afterwards
        is what the caller sees; the run's own record is honest by then, one way or the other.
        """
        pointer = NudgePointer.at_document(str(document), note)
        # An adapter that takes its prompt on its command line is launched with it; one that comes
        # up with an empty composer is pointed at the same document afterwards. The difference is
        # the adapter's, and `HeadSpec.prompt_after_start` is where the product already records it.
        prompt = None if spec.prompt_after_start else pointer.text
        spawned = False
        try:
            try:
                rendered = render_head_command(profile, prompt=prompt, workspace=run.workspace, role="")
            except HeadCommandError as exc:
                raise ValidationRefused(
                    f"this run's head command could not be rendered: {exc}"
                ) from None
            receipt = self.runtime.start(
                spec,
                run.workspace,
                self._task_ref(run, document),
                command=rendered.command,
                title=f"product-run:{run.run_id}",
                run_id=run.run_id,
                role=run.role,
                env=env,
                subject=f"product-run:{run.run_id}",
            )
            if receipt.status == HEAD_BUSY:
                raise OwnerConflict(f"a head is already up for run {run.run_id}: {receipt.reason}")
            if not receipt.ok or receipt.run is None:
                raise RuntimeUnavailable(
                    "the product runtime could not raise this run's head: "
                    f"{receipt.reason or receipt.status}"
                )
            # From here a process provably exists: the backend said so about this very run, and
            # nothing below may close this run without ending that process and confirming it.
            spawned = True
            live = receipt.run
            if spec.prompt_after_start:
                live = self._point_at_the_task(live, run, pointer)
            return self._save(
                run.with_head(
                    live.to_json(),
                    phase=RAISED,
                    head_pid=_pid_of(run),
                    supervisor_pid=_supervisor_pid_of(run),
                    head_raised=True,
                )
            )
        except BaseException as exc:
            with contextlib.suppress(ReadError, RunStoreError):
                # Through `advance` and not straight into `_close`: this is a close like any other,
                # and the claim that one function owns every close has to be true of this one too.
                self.advance(
                    run,
                    SETTLED,
                    now=now,
                    reason=STOP_BRING_UP_FAILED,
                    failure=f"this run's head could not be raised: {exc}",
                    spawned=spawned,
                )
            raise

    # -- anything -> settled, or honestly to unresolved ----------------------------------------

    def _close(
        self,
        run: ProductRun,
        *,
        now: float,
        reason: str = STOP_ENDED,
        failure: str = "",
        spawned: bool = False,
        observed: dict[str, Any] | None = None,
    ) -> ProductRun:
        """End whatever head this run may hold, confirm it, and settle exactly once.

        The order is the invariant. The head is ended and the ending is **confirmed from disk**
        before anything terminal is written, because a record that says `process_failed` over a
        process that is still running is worse than a record that says it does not know: the first
        frees the card for a second run beside a live head, and the second does not.
        """
        if self._may_hold_a_process(run, spawned=spawned):
            confirmed, detail = self._end_the_head(run, reason)
            if not confirmed:
                return self._save(run.in_doubt(detail))
        state = observed if observed is not None else run_state_reads.observe(run, now=now)
        value, why = self._ending(state, failure=failure, reason=reason)
        exit_status, result = run_state_reads.terminal_evidence(run)
        try:
            settled, _first = self.store.settle(
                run.run_id, value, why, now=now, exit_status=exit_status, result=result
            )
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None
        return settled

    def _ending(self, state: dict[str, Any], *, failure: str, reason: str) -> tuple[str, str]:
        """What this run is recorded as having ended as, and why.

        A bring-up that failed says so in its own words. Otherwise the process evidence decides,
        and an ending the evidence cannot name a terminal value for is `process_failed` with the
        reason the product ended it: by the time this is reached the head has been stopped and
        confirmed gone, so "still running" is not one of the answers available.
        """
        if failure:
            return run_state_reads.PROCESS_FAILED, failure
        if state.get("terminal"):
            return str(state["value"]), str(state["reason"])
        return run_state_reads.PROCESS_FAILED, (
            f"{reason}; its head was ended and confirmed gone, and the process evidence named no "
            f"terminal state of its own ({state.get('reason') or state.get('value')})"
        )

    def _may_hold_a_process(self, run: ProductRun, *, spawned: bool) -> bool:
        """Whether a head may exist under this run, decided conservatively and from disk.

        Three witnesses, and any one of them is enough:

        * `spawned` -- the backend returned a successful receipt for this start, in *this* call.
          The one fact no later reader could reconstruct, and the reason the caller passes it down;
        * the **phase** -- `raised` or `unresolved` on disk is the durable form of the same fact,
          for a run some other process raised;
        * the **trace** -- a pid file or a supervisor journal in the run directory. This is what
          answers for a start that failed in a way the backend did not classify: by the time such
          an exception is raised, a supervisor that got as far as running has written at least one
          of them.

        A start the backend itself reported as failed, over a run directory holding neither
        artefact, is the one case answered "no", and it is answered from two independent witnesses
        rather than from control flow alone.
        """
        if not run.addressable:
            return False
        if spawned or run.phase in (RAISED, UNRESOLVED):
            return True
        return self._left_a_trace(run)

    def _left_a_trace(self, run: ProductRun) -> bool:
        for candidate in (run.pid_file, run.journal_path):
            if candidate and Path(candidate).exists():
                return True
        return False

    def _end_the_head(self, run: ProductRun, reason: str) -> tuple[bool, str]:
        """Stop this run's head from the record alone, and say whether the ending was confirmed.

        Confirmation is the backend's, and it is the launch identity going dead rather than a
        socket disappearing. Nothing here is believed on the strength of having asked.
        """
        try:
            head = HeadRun.from_json(run.head_run)
        except (HeadRunError, ValueError, TypeError) as exc:
            return False, (
                "this run's head record could not be read back, so its process could not be ended "
                f"and may still be running: {exc}"
            )
        try:
            receipt = self.runtime.stop(head, StopInitiator(INITIATOR, reason))
        except Exception as exc:  # noqa: BLE001 - a backend failure is an unconfirmed cleanup
            return False, (
                f"the stop of this run's head failed ({type(exc).__name__}: {exc}), so its process "
                "may still be running"
            )
        if getattr(receipt, "status", "") == HEAD_OK:
            return True, ""
        return False, (
            "this run's head was asked to stop and its ending could not be confirmed "
            f"({getattr(receipt, 'reason', '') or getattr(receipt, 'status', 'no answer')}), so its "
            "process may still be running"
        )

    # -- the pieces a bring-up is made of -------------------------------------------------------

    def _task_ref(self, run: ProductRun, document: Path) -> TaskRef:
        return TaskRef.card(run.ref, document=str(document))

    def _preflight(self, run: ProductRun, *, spec: HeadSpec, profile: dict[str, Any]) -> None:
        """Prepare the workspace for the head about to be raised into it, on its own runtime.

        The same two preparations the pipeline makes, reached directly rather than through the
        pane driver that also makes them: Codex' workspace trust is a hard precondition without
        which the TUI never reaches readiness, and Claude's trust and theme are best-effort. Both
        happen in the write-ahead phase, before a spawn, so a preparation that fails closes a run
        that provably holds no process.
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

    def _point_at_the_task(self, live: HeadRun, run: ProductRun, pointer: NudgePointer) -> HeadRun:
        """Hand an interactive head its task, once it is actually ready to read one.

        The `start` verb can carry the pointer itself, and for this product it must not: an
        interactive TUI comes up over several seconds -- it draws its banner, starts its MCP servers
        and only then owns its composer -- and a line delivered into it before that lands in the
        composer *without being submitted*. The delivery is then perfectly true (every byte reached
        the terminal, and the substrate says so) and the head sits idle with its prompt unsent in
        front of it, which is exactly the failure this product runtime exists not to have: a run
        that reads as started and is not.

        So the head is raised bare and then driven the way a keyboard drives one, in four steps:

        1. **wait until it stops printing.** Read through the backend's own `observe`, off the
           supervisor's count of the bytes the head has produced -- the only thing that moves while
           a TUI draws itself, because no turn is open yet and the journal records none;
        2. **deliver the line.** It lands in the composer, whole, and sends nothing;
        3. **wait until the backend says the head is idle again.** The substrate opens a turn of its
           own for every payload it carries and closes it when the head goes quiet, so a second
           delivery made straight away is refused `HEAD_BUSY` by a turn that is about the bytes
           rather than about the agent -- and a refusal accepted as success is a prompt nobody sent;
        4. **deliver `SUBMIT_KEY` as its own payload.** One burst carrying the line and its
           carriage return is read as a paste and sends nothing, so Enter has to arrive by itself.

        `settle_seconds` bounds both waits. A head that never quietens still gets its line and its
        Enter, and the receipts are what say whether either landed: holding a run open with nothing
        in it is not the better failure.
        """
        subject = f"product-run:{run.run_id}"
        self._wait_until_quiet(live)
        delivered = self.runtime.deliver(live, pointer, subject=subject)
        if not getattr(delivered, "arrived", delivered.ok):
            raise RuntimeUnavailable(
                "this run's head came up and its task could not be put in front of it: "
                f"{delivered.reason or delivered.status}"
            )
        live = delivered.run or live
        self._wait_until_idle(live)
        sent = self.runtime.deliver(live, NudgePointer.line(SUBMIT_KEY), subject=f"{subject}:submit")
        if not getattr(sent, "arrived", sent.ok):
            raise RuntimeUnavailable(
                "this run's head was given its task and could not be told to send it: "
                f"{sent.reason or sent.status}"
            )
        return sent.run or live

    def _wait_until_quiet(self, live: HeadRun) -> None:
        """Wait until the head has stopped printing, or until this bring-up's bound runs out."""
        deadline = time.monotonic() + self.settle_seconds
        printed = -1
        steady_since = time.monotonic()
        while time.monotonic() < deadline:
            receipt = self.runtime.observe(live)
            evidence = receipt.evidence if isinstance(receipt.evidence, dict) else {}
            current = evidence.get("output_bytes")
            current = current if isinstance(current, int) else -1
            if current != printed:
                printed, steady_since = current, time.monotonic()
            elif printed > 0 and time.monotonic() - steady_since >= self.settle_quiet_seconds:
                return
            time.sleep(self.settle_poll_seconds)

    def _wait_until_idle(self, live: HeadRun) -> None:
        """Wait until the backend will take another payload for this head.

        `busy` is the backend's own answer and covers both halves of what would refuse the next
        delivery: the substrate's turn over the payload just carried, and the turn lease this
        runtime granted for it. A backend that cannot say (`busy` is `None`) is not waited on --
        an unknown is not a yes, and the delivery below reports its own refusal if there is one.
        """
        deadline = time.monotonic() + self.settle_seconds
        while time.monotonic() < deadline:
            if not self.runtime.observe(live).busy:
                return
            time.sleep(self.settle_poll_seconds)

    # -- the store, with this layer's own failure vocabulary ------------------------------------

    def _save(self, run: ProductRun) -> ProductRun:
        try:
            return self.store.save(run)
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None


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
    decides whether the run is alive -- that is the classified heartbeat, in `run_state`.
    """
    try:
        record = json.loads(Path(run.pid_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    value = record.get(name) if isinstance(record, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
