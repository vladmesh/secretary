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
    finish?" a question the record answers rather than one an operator reconstructs from timing.
    The first initiator is also the one that stays: a stop that was refused is retried by later
    ticks, often through another path with another actor, and a transition that overwrote the
    initiator would leave the record naming whoever retried last rather than whoever began. So
    `finishing` is idempotent — entering it again returns the same run, initiator included;
  * **it is JSON, so it survives the dispatcher.** A run is durable state: the process that spawned
    a head is not necessarily the process that stops it, and the initiator has to still be there
    after a restart, which is the whole point of writing it down.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
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

# This is deliberately a small, transport-neutral vocabulary.  The Codex preflight owns the
# evidence that can enter ``allowed``; HeadRun owns the durable rule that a historical or damaged
# record is never accidentally read back as that state.
FANOUT_POLICY_VERSION = 1
FANOUT_ALLOWED = "allowed"
FANOUT_UNKNOWN = "unknown"
FANOUT_VIOLATION = "violation"
# ``schema_absent`` and ``schema_unknown`` are separately durable so an operator can tell an
# omitted capture from an unreadable one.  Neither is a terminal clean state.
FANOUT_SCHEMA_ABSENT = "schema_absent"
FANOUT_SCHEMA_UNKNOWN = "schema_unknown"
FANOUT_POLICY_STATES = (
    FANOUT_ALLOWED,
    FANOUT_UNKNOWN,
    FANOUT_VIOLATION,
    FANOUT_SCHEMA_ABSENT,
    FANOUT_SCHEMA_UNKNOWN,
)


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
    # Role is part of the provider attestation, not inferred from a profile id.  The empty value
    # remains readable for records produced before this field existed, but it is not usable as
    # allow evidence.
    role: str = ""
    handle: str = ""
    leaf: str = ""
    pid_file: str = ""
    lifecycle: str = SPAWNED
    stopped_by: StopInitiator | None = None
    # A versioned Codex provider fan-out policy record.  ``to_json`` and ``from_json`` both pass
    # it through ``_fanout_policy_json`` so malformed or historical state stays explicitly
    # unknown rather than being silently omitted by a later lifecycle write.
    fanout_policy: dict[str, Any] = field(default_factory=dict)

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
        # Frozen records still need a canonical value on their first serialisation.  Without this
        # a fresh historical-shaped run and the same run read back from JSON compare differently,
        # inviting callers to treat the latter normalisation as a lifecycle change.
        policy = _fanout_policy_json(self.fanout_policy)
        if policy.get("state") == FANOUT_ALLOWED and (
            policy.get("run_id") != self.run_id
            or policy.get("role") != self.role
            or policy.get("model") != (self.spec.model or "")
        ):
            policy = _unknown_fanout_policy("fan-out policy binding does not match this HeadRun")
        object.__setattr__(self, "fanout_policy", policy)

    @property
    def running(self) -> bool:
        """Whether this run still expects a process behind it."""
        return self.lifecycle in (SPAWNED, WORKING)

    @property
    def settled(self) -> bool:
        """Whether this head's end was confirmed, so nothing is owed to it any more.

        Deliberately not `not running`: `finishing` is neither. It is a head whose stop was begun
        and not confirmed, which is exactly the state a later tick must continue rather than
        replace — a caller that reads "not running" as "finished with" throws away the identity
        and the initiator of a stop that is still owed, and the retry then becomes a new stop of a
        head nothing can name.
        """
        return self.lifecycle == EXITED

    def same_run(self, other: "HeadRun") -> bool:
        """Whether two values name the same head, whatever pane handle each of them is holding."""
        return self.run_id == other.run_id

    @property
    def fanout_policy_state(self) -> str:
        """The terminal provider policy state, conservatively normalised on every read."""
        return str(_fanout_policy_json(self.fanout_policy).get("terminal_state") or FANOUT_UNKNOWN)

    @property
    def fanout_clean(self) -> bool:
        """Whether this exact run has independently-attested, still-clean provider evidence."""
        policy = _fanout_policy_json(self.fanout_policy)
        return (
            policy.get("state") == FANOUT_ALLOWED
            and policy.get("terminal_state") == "clean"
            and policy.get("run_id") == self.run_id
            and policy.get("role") == self.role
            and policy.get("model") == (self.spec.model or "")
        )

    def with_fanout_policy(self, policy: Any) -> "HeadRun":
        """Return this run with a conservatively serialisable policy attestation."""
        return replace(self, fanout_policy=_fanout_policy_json(policy))

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

        Idempotent by initiator, and that is the whole of the rule: the first actor to enter this
        state is the one the record keeps. A refused stop is retried — by the next tick, by
        reconciliation, by an operator — and each of those arrives with an initiator of its own.
        Overwriting would make the record name the last retry rather than the decision that ended
        the head, which is the one question this field exists to answer.
        """
        if not isinstance(initiator, StopInitiator):
            raise HeadRunError("a stop initiator is a StopInitiator")
        if self.lifecycle in (FINISHING, EXITED):
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
            "role": self.role,
            "handle": self.handle,
            "leaf": self.leaf,
            "pid_file": self.pid_file,
            "lifecycle": self.lifecycle,
            "stopped_by": self.stopped_by.to_json() if self.stopped_by else {},
            "fanout_policy": _fanout_policy_json(self.fanout_policy),
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
            role=str(payload.get("role") or ""),
            handle=str(payload.get("handle") or ""),
            leaf=str(payload.get("leaf") or ""),
            pid_file=str(payload.get("pid_file") or ""),
            lifecycle=str(payload.get("lifecycle") or SPAWNED),
            stopped_by=StopInitiator.from_json(payload.get("stopped_by")),
            fanout_policy=_fanout_policy_json(payload.get("fanout_policy")),
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


def _fanout_policy_json(payload: Any) -> dict[str, Any]:
    """Return one safe policy shape, never upgrading unknown history into an allow.

    This function intentionally does not try to recreate an attestation from profile data.  The
    evidence belongs to the run that was launched, and a recovery process has no authority to
    manufacture it from today's registry or a screen transcript.
    """
    if not isinstance(payload, dict):
        return _unknown_fanout_policy("fan-out policy attestation is missing")
    version = payload.get("version")
    if version != FANOUT_POLICY_VERSION:
        return _unknown_fanout_policy("fan-out policy attestation has an unsupported version")
    state = str(payload.get("state") or "")
    terminal_state = str(payload.get("terminal_state") or "")
    if state not in FANOUT_POLICY_STATES or terminal_state not in ("clean", FANOUT_UNKNOWN, FANOUT_VIOLATION):
        return _unknown_fanout_policy("fan-out policy attestation is malformed")
    result = dict(payload)
    result["version"] = FANOUT_POLICY_VERSION
    result["state"] = state
    result["terminal_state"] = terminal_state
    events = result.get("events")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        return _unknown_fanout_policy("fan-out policy event log is malformed")
    result["events"] = [dict(event) for event in events]
    for event in result["events"]:
        if (
            str(event.get("type") or "") not in {
                "collaboration_call", "child_thread_edge", "unknown_thread_edge",
                "unparseable_provider_event",
            }
            or not str(event.get("raw_event_digest") or "")
            or event.get("source_sequence") is None
            or not str(event.get("source_location") or "")
            or not str(event.get("captured_at") or "")
        ):
            return _unknown_fanout_policy("fan-out policy event log is malformed")
    source_required = result.get("provider_source_required") is True
    source = result.get("provider_source")
    if source_required and source is None:
        return _unknown_fanout_policy(
            "fan-out provider source binding is missing", provider_source_required=True
        )
    if source is not None:
        if not isinstance(source, dict):
            return _unknown_fanout_policy(
                "fan-out provider source binding is malformed", provider_source_required=True
            )
        source_version = source.get("version")
        source_state = str(source.get("state") or "")
        if source_version != 1 or source.get("kind") != "codex_session_event_jsonl":
            return _unknown_fanout_policy(
                "fan-out provider source binding has an unsupported version", provider_source_required=True
            )
        if source_state == "unbound":
            if not str(source.get("root") or "") or not isinstance(source.get("baseline"), list):
                return _unknown_fanout_policy(
                    "fan-out provider source baseline is malformed", provider_source_required=True
                )
        elif source_state == "bound":
            cursor = source.get("cursor")
            initial_range = source.get("initial_range")
            first = initial_range.get("first") if isinstance(initial_range, dict) else None
            root = initial_range.get("root") if isinstance(initial_range, dict) else None
            last = initial_range.get("last") if isinstance(initial_range, dict) else None
            if (
                not str(source.get("root") or "")
                or not str(source.get("path") or "")
                or not str(source.get("session_id") or "")
                or not str(source.get("parent_thread_id") or "")
                or not isinstance(cursor, dict)
                or not isinstance(cursor.get("line"), int)
                or cursor.get("line") < 0
                or not _digest(cursor.get("digest"))
                or not isinstance(first, dict)
                or first.get("line") != 1
                or not _digest(first.get("digest"))
                or not isinstance(root, dict)
                or not isinstance(root.get("line"), int)
                or root.get("line") < first.get("line")
                or not _digest(root.get("digest"))
                or not isinstance(last, dict)
                or not isinstance(last.get("line"), int)
                or last.get("line") < root.get("line")
                or not _digest(last.get("digest"))
                or not _digest(initial_range.get("digest"))
                or not str(source.get("bound_at") or "")
            ):
                return _unknown_fanout_policy(
                    "fan-out provider source binding is malformed", provider_source_required=True
                )
        else:
            return _unknown_fanout_policy(
                "fan-out provider source binding is malformed", provider_source_required=True
            )
    if state == FANOUT_ALLOWED and terminal_state == "clean" and result["events"]:
        return _unknown_fanout_policy("a clean fan-out policy record carries provider events")
    # The only shape that could be read as clean needs its complete binding.  A damaged historic
    # record consequently stays visible and safely non-clean after the next ordinary write.
    if state == FANOUT_ALLOWED and (
        not str(result.get("run_id") or "")
        or not str(result.get("role") or "")
        or not str(result.get("model") or "")
        or not str(result.get("binary_path") or "")
        or not _digest(result.get("binary_digest"))
        or not str(result.get("cli_version") or "")
        or not _digest(result.get("tool_schema_digest"))
        or result.get("provider_schema_verdict") != "no_callable_child_spawn_surface"
    ):
        return _unknown_fanout_policy("fan-out policy allow attestation is incomplete")
    return result


def _digest(value: Any) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)


def _unknown_fanout_policy(
    reason: str, *, provider_source_required: bool = False
) -> dict[str, Any]:
    result = {
        "version": FANOUT_POLICY_VERSION,
        "state": FANOUT_UNKNOWN,
        "terminal_state": FANOUT_UNKNOWN,
        "reason": reason,
        "events": [],
    }
    if provider_source_required:
        # Keep enough typed provenance for the runtime to route the damaged binding through the
        # exact run's ingress.  Replacing it with absence would let recovery skip the fence.
        result["provider_source_required"] = True
        result["provider_source"] = {}
    return result
