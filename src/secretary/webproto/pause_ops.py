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
        result = self._call(lambda: _pause(runtime, mode=DRAIN, actor=actor, reason=reason))
        return self._document(PAUSE_DRAIN_OPERATION, result, actor=actor, now=now, restored=None)

    def pause_resume(self, *, actor: str) -> dict[str, Any]:
        """Clear the pause, and report what was actually put back.

        `resume` is the one that owns this: a drain stopped nothing and so puts nothing back, a
        freeze relaunches the worker and reviewer heads it stopped in their existing workspaces, and
        a card whose head reported while the pause was on is left to the next tick rather than given
        a fresh head. What this adds is the reporting: the mode that was lifted and the three lists
        the resume produced, told apart from the no-op of resuming a pipeline that was not paused.
        """
        now = self._clock()
        runtime = self._runtime()
        result = self._call(lambda: _resume(runtime, actor=actor))
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
        except DispatcherError as exc:
            raise _CODES.get(exc.code, RuntimeUnavailable)(exc.message) from None
        except HostError as exc:
            raise RuntimeUnavailable(f"the host could not answer this pause command: {exc}") from None

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


def _restored(result: dict[str, Any]) -> dict[str, Any]:
    """What a resume put back, from the lists the resume itself produced.

    Narration over `resume`'s own answer and no decision of its own: the lists are copied, and the
    sentence only says which of the three cases they belong to. The drain case is the one worth
    spelling out -- an empty `relaunched` there is not a resume that failed to bring anything back,
    it is a drain that never stopped anything.
    """
    mode = str(result.get("resumed_mode") or "") or None
    relaunched = list(result.get("relaunched") or [])
    parked = list(result.get("parked") or [])
    skipped = list(result.get("skipped") or [])
    if mode is None:
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
        "observers_resumed": list(result.get("observers_resumed") or []),
        "statement": statement,
    }


__all__ = [
    "PAUSE_DRAIN_OPERATION",
    "PAUSE_RESUME_OPERATION",
    "PauseOperationLayer",
]
