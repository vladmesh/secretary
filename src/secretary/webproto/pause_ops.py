"""The two pause operations of this layer: put the pipeline into a soft pause, and lift it.

`pause_drain` sets the pipeline-wide soft pause. `pause_resume` clears whatever pause is set and
puts back what a freeze stopped. That is the whole surface, and what is *not* on it is as much of
the contract as what is:

**There is no freeze operation and no mode parameter.** `pause_drain` cannot be asked for anything
but a drain: it takes no mode, has no default mode, no fallback, no retry in another mode and no
convenience flag, and it passes the literal :data:`~secretary.webproto.pause_reads.DRAIN` down. A
freeze stops live heads -- including the head that would be calling this -- so the one path to one
is the deliberate `secretary pause freeze`, and the existing refusal to change mode while paused
(`dispatcher_pause_ops.pause`'s `pause_conflict`) keeps a drain from being turned into one behind
the caller's back. That refusal is preserved here rather than smoothed over: it reaches the caller
as `owner_conflict`, which is what a well-formed request refused on the state of the world is in
this layer.

**Every rule stays in `secretary.dispatcher_pause_ops`.** The tick lock, the idempotent-in-the-same-
mode no-op, the conflict, the legacy mirror, the head stop and relaunch, the watchdog windows and
the auto-resume TTL are all its, and these operations call it and re-decide none of them. There is
no second flag, no second lock, no second store and no request index of this layer's own: the pause
is idempotent in its own mode by its own rule, so a repeat needs no key to be safe.

**The result of a command is readable, and its three outcomes are told apart.** `action` is the
answer `dispatcher_pause_ops` itself gave -- `paused` for a command that set the flag, `noop` for a
repeat in the same mode that changed nothing, `resumed` for a resume that lifted one -- and
`changed` is that as a boolean. A refusal is not an action at all: `pause_conflict` raises
`OwnerConflict` and writes nothing. Beside them the document carries the state read
(:meth:`~secretary.webproto.pause_reads.PauseReadLayer.pause_state`), so a caller reads what the
pipeline is now from the same sections it would have read before the call.

**And a command that did something says so even when the pipeline cannot be described afterwards.**
`dispatcher_pause_ops.pause` and `resume` set the flag and then render the status through
`pause_status`, which converts every dispatcher record -- so a production state that is semantically
corrupt (an obsolete record shape, an `attempt_round` that is not an integer, a truncated write)
makes that last step refuse over a pause that has already taken. What this layer reports then is the
action the command itself decided under the production tick lock:
`dispatcher_pause_ops.PauseCommandCompleted` carries that decision out of the lock with the failure
of the render, and :meth:`PauseOperationLayer._perform` reports it with the refusal as a warning and
an unavailable section on the embedded state read.

**The action is never inferred here, and cannot be.** This layer reads no flag of its own, before or
after a command. A flag read taken outside the lock is not evidence of which command set what it
holds: with the pipeline already drained, a second `pause_drain` that observes `drain` on both sides
of its call may have found the mode already held, or may have written its own drain after another
command's `resume` cleared the flag between those two reads -- the same two observations for two
different actions (secretary-1577, reproduced on secretary-1576's round 4). So the decision comes
from where the command was serialised against every other one, and nothing else is consulted.
`_perform` re-decides nothing and repairs nothing: a failure that is not a completed command travels
unchanged, and a refusal of the pause's own rules -- `validation`, `pause_conflict` -- never reaches
that path at all, because those are decisions made before anything is written.

**And a resume says what it put back**, from the lists the stop itself wrote: `relaunched`, `parked`
and `skipped` as `resume` produced them, with the mode that was lifted. A drain relaunches nothing
because a drain stopped nothing, and the document says that in words rather than leaving an empty
list to be read as a failure.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary.dispatcher import runtime_from_args
from secretary.dispatcher_pause_ops import PauseCommandCompleted
from secretary.dispatcher_pause_ops import pause as _pause
from secretary.dispatcher_pause_ops import resume as _resume
from secretary.dispatcher_types import DispatcherError, HostError
from secretary.webproto import sources
from secretary.webproto.boundary import ProtocolBoundary
from secretary.webproto.errors import OwnerConflict, RuntimeUnavailable, ValidationRefused
from secretary.webproto.pause_reads import (
    DRAIN,
    DRAIN_CONTRACT,
    FREEZE_CONTRACT,
    SCHEMA_VERSION,
    PauseReadLayer,
    extent,
)

#: The operations of this module, named so a client can offer them without spelling either twice.
PAUSE_DRAIN_OPERATION = "pause_drain"
PAUSE_RESUME_OPERATION = "pause_resume"

#: The error contract of the pause half, in one place, for the four operations of both its modules.
#:
#: It is here because a published contract that only a document states goes stale on the next change
#: -- twice on this card a behaviour change left a public sentence behind. So the codes each
#: operation can refuse with are a value: `tests/test_web_pause_protocol.py` drives every code listed
#: here out of the real operation, *and* checks the operations table in `docs/PROTOCOLS.md` against
#: it, so the prose fails with the code rather than after it.
#:
#: What is deliberately not listed: `backend_unavailable` reaching a caller from
#: :mod:`secretary.webproto.boundary` for an implementation failure anywhere in this layer. That is
#: the layer-wide contract every operation of this package carries and not something a pause
#: operation decides, and listing it per operation would be listing it everywhere.
PAUSE_ERRORS: dict[str, tuple[str, ...]] = {
    PAUSE_DRAIN_OPERATION: ("validation", "owner_conflict", "backend_unavailable"),
    PAUSE_RESUME_OPERATION: ("validation", "backend_unavailable"),
    "pause_state": ("validation",),
    "pause_scope": ("validation",),
}

#: How a `DispatcherError` from the pause becomes a code of this layer. Every entry is a mapping and
#: never a re-decision: what was refused and why is `dispatcher_pause_ops`' answer, and this only
#: says which of this layer's codes carries it.
#:
#: `pause_conflict` is `owner_conflict` for the reason that code exists. The request is well formed
#: -- a drain, an actor, a reason -- and it is refused on the state of the world: the pipeline is
#: already paused in the other mode. The same request made after a resume is admitted, exactly as a
#: comment on a closed sprint is.
#: `invalid_instance` and `invalid_heads` are `validation` for a compatibility reason, and it is the
#: one this card had to be told twice: these three commands reached the dispatcher through
#: `runtime_from_args`, whose refusal of a config that does not validate is a `DispatcherError` with
#: exit status 2, and an operator or script reading that status must keep reading it now that the
#: command is a client of this layer. It is also the honest code: the caller named an installation
#: that is not one, which is a malformed request and not a durable source of this installation
#: refusing.
_CODES: dict[str, Any] = {
    "validation": ValidationRefused,
    "usage": ValidationRefused,
    "pause_conflict": OwnerConflict,
    "invalid_instance": ValidationRefused,
    "invalid_heads": ValidationRefused,
}


class PauseOperationLayer(ProtocolBoundary):
    """One installation's pause operations, with no knowledge of who is asking.

    Construction does no I/O: the dispatcher runtime the operations act through is built when one is
    called. `runtime` is the seam a test -- or a caller that already holds one -- supplies its own
    through; it is not a mode, and the same code path runs against the live installation.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        runtime: Any | None = None,
        board_client: Any | None = None,
        clock: Callable[[], float] = time.time,
        host_mode: str = "real",
        owner: str = "secretary-dispatcher",
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._given_runtime = runtime
        self._board_client = board_client
        self._clock = clock
        self._host_mode = host_mode
        self._owner = owner

    # -- operations ---------------------------------------------------------------------------

    def pause_drain(self, *, actor: str, reason: str) -> dict[str, Any]:
        """Put the pipeline into the soft pause, or say that it already was in it.

        There is no `mode` argument, and that is the contract rather than an omission: this
        operation can produce a drain and nothing else. The literal is handed to
        `dispatcher_pause_ops.pause`, which owns every rule about what a pause is -- the tick lock,
        the same-mode no-op, and the refusal to change mode while paused.

        A drain stops no running head. The card whose worker is writing right now keeps writing;
        what stops is claiming new cards and dispatching background roles. The document says so on
        its `modes.drain` object, and the heads that keep running are listed on the state read
        beside it.
        """
        now = self._clock()
        runtime = self._runtime()
        result = self._perform(lambda: _pause(runtime, mode=DRAIN, actor=actor, reason=reason))
        return self._document(PAUSE_DRAIN_OPERATION, result, actor=actor, now=now, restored=None)

    def pause_resume(self, *, actor: str) -> dict[str, Any]:
        """Clear the pause, and report what was actually put back.

        `resume` is the one that owns this: a drain stopped nothing and so puts nothing back, a
        freeze relaunches the worker and reviewer heads it stopped in their existing workspaces, and
        a card whose head reported while the pause was on is left to the next tick rather than given
        a fresh head. What this adds is the reporting: the mode that was lifted and the three lists
        the resume produced, told apart from the no-op of resuming a pipeline that was not paused.

        A resume whose own answer never arrived (:meth:`_perform`) still reports the pause it lifted,
        with those lists `null` for a freeze: what it put back was in the answer nobody could read,
        which is not the empty list's claim that it put nothing back.
        """
        now = self._clock()
        runtime = self._runtime()
        result = self._perform(lambda: _resume(runtime, actor=actor))
        return self._document(
            PAUSE_RESUME_OPERATION,
            result,
            actor=actor,
            now=now,
            restored=_restored(result),
        )

    def _runtime(self) -> Any:
        """The dispatcher runtime these operations act through, built once per call.

        The same one `secretary dispatcher` builds, through the same factory, so the operation and
        the CLI cannot act on two different dispatchers. A config this factory refuses is an
        installation that cannot be paused at all, and it reaches the caller as
        `backend_unavailable` rather than as the dispatcher's own vocabulary.
        """
        if self._given_runtime is not None:
            return self._given_runtime
        return self._call(
            lambda: runtime_from_args(
                str(self.instance),
                str(self._data_dir) if self._data_dir is not None else None,
                host_mode=self._host_mode,
                owner=self._owner,
            )
        )

    # -- the pieces an operation is made of ----------------------------------------------------

    @staticmethod
    def _call(operation: Callable[[], Any]) -> Any:
        """Run one call into the dispatcher, and translate its vocabulary once.

        `DispatcherError` is mapped by :data:`_CODES` and never re-decided; a `HostError` is the
        product runtime failing to reach a head, which is `backend_unavailable` here as it is
        everywhere else in this layer.
        """
        try:
            return operation()
        except PauseCommandCompleted:
            # Not a failure of the command: the command happened, and this carries what it did. The
            # caller of `_call` that knows what to do with it is `_perform`; translating it here
            # would turn a completed pause into `backend_unavailable` again.
            raise
        except DispatcherError as exc:
            raise _CODES.get(exc.code, RuntimeUnavailable)(exc.message) from None
        except HostError as exc:
            raise RuntimeUnavailable(f"the host could not answer this pause command: {exc}") from None

    def _perform(self, operation: Callable[[], Any]) -> dict[str, Any]:
        """One pause command, and its own answer to "what did I do" when the state cannot be read.

        `dispatcher_pause_ops.pause` and `resume` write the flag and *then* render the status
        through `pause_status`, which converts every dispatcher record. So a production state that
        no longer converts -- a record shape this release does not store, an `attempt_round` that is
        not an integer, a file a partial write truncated -- raises after the pause has already
        taken. That ordering is the dispatcher's and this does not change it; what this changes is
        what the caller hears, because a completed drain reported as `backend_unavailable` tells an
        operator the safety control did not take while it silently did, in exactly the situation the
        command exists for.

        So the dispatcher hands the failure over with the decision it made under the tick lock
        attached (:class:`~secretary.dispatcher_pause_ops.PauseCommandCompleted`), and this reports
        that decision. There is nothing to establish here and nothing is read to establish it: the
        action is `paused`, `noop` or `resumed` as the code that performed it decided, and what a
        flag holds now is somebody else's command as much as it is this one's.

        Everything else travels unchanged. A `validation` refusal and a `pause_conflict` are made
        before anything is written and are re-raised as themselves; any other failure is a command
        that did not complete, and it reaches the caller as the refusal it is.
        """
        try:
            return self._call(operation)
        except PauseCommandCompleted as completed:
            return {
                **completed.decision,
                # The lists a freeze's resume produced were in the answer that never arrived. Said
                # as unknown rather than as empty, which would be the claim that it put nothing back.
                "reported": False,
                "warnings": [
                    *completed.warnings,
                    (
                        f"the command completed -- {_did(completed.decision)} -- but the dispatcher "
                        f"could not render the pipeline state afterwards: {completed.cause}."
                        " What could be established is on `state`, where the source that did not "
                        "answer is marked unavailable"
                    ),
                ],
            }

    def _document(
        self,
        operation: str,
        result: dict[str, Any],
        *,
        actor: str,
        now: float,
        restored: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """What a pause command answers with: what it did, and the pipeline as the read reads it.

        The pause is not described a second time here. A caller that has just drained and a caller
        looking an hour later read the same sections, which is why the state read is embedded at
        all: right after this call it says the flag is down, in which mode, by whom, and which heads
        are still running -- and the last of those is the one a soft pause must never be read as
        having stopped.
        """
        action = str(result.get("action") or "")
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "pause_command",
            "observed_at": sources.isoformat(now),
            "operation": operation,
            "actor": actor,
            # The three outcomes, told apart. `noop` is a repeat that changed nothing, and it is
            # deliberately not the same answer as a command that did something; a refusal is
            # neither, and never reaches here at all.
            "action": action,
            "changed": action not in {"noop", ""},
            "restored": restored,
            # Whatever the pause itself wanted the operator to hear -- an observer head the host
            # would not stop, a production state it could not read -- carried through unchanged.
            "warnings": list(result.get("warnings") or []),
            "extent": extent(),
            "modes": {"drain": DRAIN_CONTRACT, "freeze": FREEZE_CONTRACT},
            "state": self._reads().pause_state(),
        }

    def _reads(self) -> PauseReadLayer:
        """The read layer this operation answers through, built with this layer's own seams."""
        return PauseReadLayer(
            self.instance,
            data_dir=self._data_dir,
            board_client=self._board_client,
            clock=self._clock,
        )


def _did(decision: dict[str, Any]) -> str:
    """What the command did, in the words the operator's warning needs it in.

    Read off the decision the dispatcher made under the lock, and never off a state of the world:
    this only spells an action that was already established.
    """
    action = str(decision.get("action") or "")
    if action == "paused":
        return "the pipeline-wide pause was set"
    if action == "resumed":
        mode = str(decision.get("resumed_mode") or "")
        return f"the {mode} was lifted" if mode else "the pause was lifted"
    if decision.get("step") == "resume":
        return "the pipeline was not paused, so nothing was lifted"
    return "the pipeline already held the mode asked for, so nothing was written"


def _restored(result: dict[str, Any]) -> dict[str, Any]:
    """What a resume put back, from the lists the resume itself produced.

    Narration over `resume`'s own answer and no decision of its own: the lists are copied, and the
    sentence only says which of the three cases they belong to. The drain case is the one worth
    spelling out -- an empty `relaunched` there is not a resume that failed to bring anything back,
    it is a drain that never stopped anything.

    And when the resume completed but its own answer never arrived (:meth:`PauseOperationLayer.
    _perform`), the lists are `null` rather than empty for a freeze: nobody read what it put back,
    and an empty list there would be the claim that it put nothing back. For a drain and for a
    pipeline that was not paused they are still `[]`, because what was put back is established by
    what a drain is and not by a list somebody had to read.
    """
    mode = str(result.get("resumed_mode") or "") or None
    unread = not result.get("reported", True) and mode == "freeze"
    relaunched = None if unread else list(result.get("relaunched") or [])
    parked = None if unread else list(result.get("parked") or [])
    skipped = None if unread else list(result.get("skipped") or [])
    if unread:
        statement = (
            "the freeze was lifted, but the resume's own report could not be read back, so what it "
            "put back is not established here; the heads that are up now are on `state`"
        )
    elif mode is None:
        statement = "the pipeline was not paused, so this resume lifted nothing and put nothing back"
    elif mode == DRAIN:
        statement = (
            "the drain stopped no head, so there was nothing to put back; the tick claims Ready "
            "cards and dispatches background roles again"
        )
    else:
        statement = (
            f"the freeze was lifted: {len(relaunched)} head(s) were relaunched in their existing "
            f"workspaces, {len(parked)} were left to the next tick, and {len(skipped)} were not "
            "brought back"
        )
    return {
        "resumed_mode": mode,
        "relaunched": relaunched,
        "parked": parked,
        "skipped": skipped,
        "observers_resumed": None if unread else list(result.get("observers_resumed") or []),
        "statement": statement,
    }


__all__ = [
    "PAUSE_DRAIN_OPERATION",
    "PAUSE_ERRORS",
    "PAUSE_RESUME_OPERATION",
    "PauseOperationLayer",
]
