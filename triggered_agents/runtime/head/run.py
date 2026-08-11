"""`HeadRun`: one head's life, from the pane it was opened in to the initiator that ended it.

`HeadSpec` says what a head *is*; this says that one of them is running, and it is the value the
three operations hand each other. Four things about it are decisions rather than fields:

  * **identity is its own value, not the pane handle.** Orca aliases the handle it returned at
    create time while the pty stays exactly where it was, and a product that identifies a run by
    its handle reads that reincarnation as a head that vanished and a head that appeared. Every
    stop path in this dispatcher has had to work around it by carrying a leaf beside the handle.
    Here the run has a `run_id` that nothing about the pane can move, and `rebound` puts the new
    handle on the same run;
  * **the lifecycle is four states and moves one way.** `spawned` is a head that has a pane,
    `working` one that has been given its task, `finishing` one somebody has asked to stop, and
    `exited` one whose stop was confirmed. A tick that finds a record in `finishing` knows a stop
    was begun and not confirmed, which is precisely the state the product used to have no name for
    and answered by launching a second head beside the first;
  * **who stopped it is recorded, and it is recorded before the stop happens.** `finishing` cannot
    be entered without an initiator, so there is no way to end a head and leave the record saying
    only that it ended. That is what makes "did the watchdog kill this worker, or did the card
    finish?" a question the record answers rather than one an operator reconstructs from timing;
  * **it is JSON, so it survives the dispatcher.** A run is durable state: the process that spawned
    a head is not necessarily the process that stops it, and the initiator has to still be there
    after a restart, which is the whole point of writing it down.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any

from .spec import DEFAULT_EFFORT, HeadSpec
from .task_ref import TaskRef

# A head that has a pane and has not been given its task yet.
SPAWNED = "spawned"
# A head its task has been delivered to.
WORKING = "working"
# A head somebody has asked to stop; its initiator is on the run from this state on.
FINISHING = "finishing"
# A head whose stop was confirmed. Terminal.
EXITED = "exited"
LIFECYCLE = (SPAWNED, WORKING, FINISHING, EXITED)


class HeadRunError(RuntimeError):
    """A transition or a record that would leave a run saying something untrue about its head."""


@dataclass(frozen=True)
class StopInitiator:
    """Who ended a head, and why they said they were ending it.

    A separate type rather than a string because it is the one fact a stop cannot be performed
    without: `stop(run, initiator)` takes it positionally, so there is no call that ends a head
    anonymously and no body-level check that could be forgotten. `actor` is the agent of the stop —
    a role, a watchdog, an operator — and `reason` is free text for the record.
    """

    actor: str
    reason: str = ""

    def __post_init__(self) -> None:
        if not str(self.actor).strip():
            raise HeadRunError("a stop names who initiated it")

    def to_json(self) -> dict[str, Any]:
        return {"actor": self.actor, "reason": self.reason}

    @classmethod
    def from_json(cls, payload: Any) -> "StopInitiator | None":
        if not isinstance(payload, dict):
            return None
        actor = str(payload.get("actor") or "")
        if not actor:
            return None
        return cls(actor=actor, reason=str(payload.get("reason") or ""))


@dataclass(frozen=True)
class HeadRun:
    """One head that was started, as everything after the start has to see it.

    Frozen, and every transition returns a new value: a run is written down between ticks, and a
    record that could be mutated in place is one whose durable copy and in-memory copy disagree for
    however long the tick takes. Equality is deliberately structural, so two reads of the same
    written record compare equal; `same_run` is what asks whether two values are the same *head*.
    """

    run_id: str
    spec: HeadSpec
    workspace: str
    task_ref: TaskRef
    handle: str = ""
    leaf: str = ""
    pid_file: str = ""
    lifecycle: str = SPAWNED
    stopped_by: StopInitiator | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise HeadRunError("a head run has an identity of its own")
        if self.lifecycle not in LIFECYCLE:
            raise HeadRunError(
                f"a head run's lifecycle is one of {', '.join(LIFECYCLE)}, "
                f"not {self.lifecycle!r}"
            )
        if self.lifecycle in (FINISHING, EXITED) and self.stopped_by is None:
            raise HeadRunError(
                f"a head run in {self.lifecycle} carries the initiator that ended it"
            )

    @property
    def running(self) -> bool:
        """Whether this run still expects a process behind it."""
        return self.lifecycle in (SPAWNED, WORKING)

    def same_run(self, other: "HeadRun") -> bool:
        """Whether two values name the same head, whatever pane handle each of them is holding."""
        return self.run_id == other.run_id

    def rebound(self, handle: str, *, leaf: str = "") -> "HeadRun":
        """The same run, addressed at the pane handle it has now.

        This is the reincarnation case and it deliberately does not touch `run_id` or the
        lifecycle: a session manager that renamed a pane has said nothing about the head in it.
        """
        return replace(self, handle=handle, leaf=leaf or self.leaf)

    def working(self) -> "HeadRun":
        """This head has been given its task."""
        if self.lifecycle in (FINISHING, EXITED):
            raise HeadRunError(f"a head in {self.lifecycle} is not given more work")
        return replace(self, lifecycle=WORKING)

    def finishing(self, initiator: StopInitiator) -> "HeadRun":
        """Somebody has asked this head to stop, and this is who.

        Recorded before the stop is attempted, on purpose: a stop that is refused or that outlives
        the dispatcher must leave the initiator behind, or the head that is still there afterwards
        is one nothing can say who was ending.
        """
        if not isinstance(initiator, StopInitiator):
            raise HeadRunError("a stop initiator is a StopInitiator")
        if self.lifecycle == EXITED:
            return self
        return replace(self, lifecycle=FINISHING, stopped_by=initiator)

    def exited(self) -> "HeadRun":
        """The stop was confirmed. Only reachable from `finishing`, which carries the initiator."""
        if self.lifecycle != FINISHING:
            raise HeadRunError(
                f"a head exits from {FINISHING}, and this run is in {self.lifecycle}"
            )
        return replace(self, lifecycle=EXITED)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "spec": _spec_json(self.spec),
            "workspace": self.workspace,
            "task_ref": self.task_ref.to_json(),
            "handle": self.handle,
            "leaf": self.leaf,
            "pid_file": self.pid_file,
            "lifecycle": self.lifecycle,
            "stopped_by": self.stopped_by.to_json() if self.stopped_by else {},
        }

    @classmethod
    def from_json(cls, payload: Any) -> "HeadRun":
        if not isinstance(payload, dict):
            raise HeadRunError("a head run is read from an object, and this is not one")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            spec=_spec_from_json(payload.get("spec")),
            workspace=str(payload.get("workspace") or ""),
            task_ref=TaskRef.from_json(payload.get("task_ref")),
            handle=str(payload.get("handle") or ""),
            leaf=str(payload.get("leaf") or ""),
            pid_file=str(payload.get("pid_file") or ""),
            lifecycle=str(payload.get("lifecycle") or SPAWNED),
            stopped_by=StopInitiator.from_json(payload.get("stopped_by")),
        )


def new_run_id() -> str:
    """An identity for one head run, unrelated to anything a session manager can rename."""
    return uuid.uuid4().hex


def _spec_json(spec: HeadSpec) -> dict[str, Any]:
    """The launch shape the head started with, written out with the run.

    Written rather than re-resolved: the registry can be edited while a head is running, and a
    record that answers "what was this head" from today's `heads.toml` describes a head that may
    never have existed.
    """
    return {
        "profile_id": spec.profile_id,
        "adapter": spec.adapter,
        "model": spec.model or "",
        "effort": spec.effort,
        "resource": spec.resource or "",
        "codex_mode": spec.codex_mode or "",
        "fallback": list(spec.fallback),
    }


def _spec_from_json(payload: Any) -> HeadSpec:
    if not isinstance(payload, dict):
        raise HeadRunError("a head run carries the spec it was launched from")
    profile_id = str(payload.get("profile_id") or "")
    adapter = str(payload.get("adapter") or "")
    if not profile_id or not adapter:
        # The same rule the spec itself holds: there is no state in which a head exists with its
        # adapter guessed, and a record that lost it is not repaired by picking one here.
        raise HeadRunError("a recorded head run names its profile and its adapter")
    fallback = payload.get("fallback")
    return HeadSpec(
        profile_id=profile_id,
        adapter=adapter,
        model=str(payload.get("model") or "") or None,
        effort=str(payload.get("effort") or DEFAULT_EFFORT),
        resource=str(payload.get("resource") or "") or None,
        codex_mode=str(payload.get("codex_mode") or "") or None,
        fallback=tuple(str(entry) for entry in fallback) if isinstance(fallback, list) else (),
    )
