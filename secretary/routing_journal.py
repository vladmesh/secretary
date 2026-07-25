"""Per-attempt routing telemetry: which head actually ran as worker and as reviewer.

Contract: docs/PROTOCOLS.md, "Routing-телеметрия попыток". The card's own metadata cannot answer
"who reviewed attempt 2": `resolved_review_head` is cleared when the card leaves Validate and the
whole routing block is reset on the way back to Ready, so the board keeps at most the last worker
head. The append-only task journal keeps every launch instead, one `routing` event per head
bring-up plus one per verdict, so a finished card still yields its worker/reviewer pairs.

A profile id alone is not a historical key: `codex`, `codex-terra`, `codex-high` and `codex-extra`
all resolve to one model with different effort, `claude-default` pins no model at all, and profiles
get re-pinned over time. So every event carries the launch configuration itself — adapter, model,
effort, codex launch mode, resource and account — snapshotted at bring-up, never re-read from
`heads.toml` afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


ROUTING_KIND = "routing"
WORKER = "worker"
REVIEWER = "reviewer"
PHASES = ("worker", "review", "verdict")


@dataclass(frozen=True)
class HeadRun:
    """One head as it was actually launched.

    `requested` is the profile the card (or the role default) asked for, `resolved` is the profile
    that really ran. They differ exactly when a fallback fired, which `fallback` states outright so
    a later diversity analysis never has to infer it from a chain in a `heads.toml` that has since
    moved on.
    """

    role: str
    requested: str
    resolved: str
    requested_from: str = "role_default"
    adapter: str = ""
    model: str = ""
    effort: str = ""
    codex_mode: str = ""
    resource: str = ""
    account: str = ""
    fallback_chain: tuple[str, ...] = ()

    @property
    def fallback(self) -> bool:
        return self.resolved != self.requested

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "requested_head": self.requested,
            "head": self.resolved,
            "requested_from": self.requested_from,
            "fallback": self.fallback,
            "fallback_chain": list(self.fallback_chain),
            "adapter": self.adapter,
            "model": self.model,
            "effort": self.effort,
            "codex_mode": self.codex_mode,
            "resource": self.resource,
            "account": self.account,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "HeadRun":
        chain = payload.get("fallback_chain")
        return cls(
            role=str(payload.get("role") or ""),
            requested=str(payload.get("requested_head") or ""),
            resolved=str(payload.get("head") or ""),
            requested_from=str(payload.get("requested_from") or "role_default"),
            adapter=str(payload.get("adapter") or ""),
            model=str(payload.get("model") or ""),
            effort=str(payload.get("effort") or ""),
            codex_mode=str(payload.get("codex_mode") or ""),
            resource=str(payload.get("resource") or ""),
            account=str(payload.get("account") or ""),
            fallback_chain=tuple(str(item) for item in chain) if isinstance(chain, list) else (),
        )


def head_run_from_profile(
    *,
    role: str,
    requested: str,
    resolved: str,
    requested_from: str,
    profile: dict[str, Any],
    resources: dict[str, Any],
    codex_mode: str = "",
) -> HeadRun:
    """Snapshot a resolved profile. `codex_mode` overrides the profile's own launch mode (a card
    can pin `codex_launch_mode`), so the record shows the mode the head really started in."""
    adapter = str(profile.get("adapter") or "")
    resource = str(profile.get("resource") or "")
    account = ""
    entry = resources.get(resource) if isinstance(resources, dict) else None
    if isinstance(entry, dict):
        account = str(entry.get("account") or "")
    mode = ""
    effort = ""
    if adapter == "codex":
        mode = str(codex_mode or profile.get("codex_mode") or "exec")
        effort = str(profile.get("effort") or "default")
    chain = profile.get("fallback")
    return HeadRun(
        role=role,
        requested=requested,
        resolved=resolved,
        requested_from=requested_from,
        adapter=adapter,
        model=str(profile.get("model") or ""),
        effort=effort,
        codex_mode=mode,
        resource=resource,
        account=account,
        fallback_chain=tuple(str(item) for item in chain) if isinstance(chain, list) else (),
    )


def routing_payload(
    *,
    attempt: int,
    attempt_id: str,
    phase: str,
    heads: Iterable[HeadRun | dict[str, Any]],
    outcome: str = "",
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"unknown routing phase {phase!r} (known: {', '.join(PHASES)})")
    return {
        "attempt": int(attempt),
        "attempt_id": attempt_id,
        "phase": phase,
        "outcome": outcome,
        "heads": [head.to_json() if isinstance(head, HeadRun) else dict(head) for head in heads],
    }


@dataclass
class AttemptRecord:
    """One worker round of a card, rebuilt from the journal."""

    attempt: int
    attempt_id: str = ""
    worker: HeadRun | None = None
    reviewer: HeadRun | None = None
    outcome: str = ""
    events: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "attempt_id": self.attempt_id,
            "worker": self.worker.to_json() if self.worker else None,
            "reviewer": self.reviewer.to_json() if self.reviewer else None,
            "outcome": self.outcome,
        }


def routing_events(events: Iterable[dict[str, Any]], reference: str = "") -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("kind") == ROUTING_KIND
        and (not reference or event.get("ref") == reference)
        and isinstance(event.get("payload"), dict)
    ]


def attempts(events: Iterable[dict[str, Any]], reference: str = "") -> list[AttemptRecord]:
    """The card's attempt sequence in journal order: who worked it, who reviewed it, how it ended.

    This is the read side of the contract — a finished card's history comes from here, not from
    board metadata, which no longer holds it.
    """
    found: dict[int, AttemptRecord] = {}
    order: list[int] = []
    for event in routing_events(events, reference):
        payload = event["payload"]
        number = payload.get("attempt")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        record = found.get(number)
        if record is None:
            record = AttemptRecord(attempt=number)
            found[number] = record
            order.append(number)
        record.attempt_id = str(payload.get("attempt_id") or record.attempt_id)
        record.events.append(str(payload.get("phase") or ""))
        for head in payload.get("heads") or []:
            if not isinstance(head, dict):
                continue
            run = HeadRun.from_json(head)
            if run.role == WORKER:
                record.worker = run
            elif run.role == REVIEWER:
                record.reviewer = run
        outcome = str(payload.get("outcome") or "")
        if outcome:
            record.outcome = outcome
    return [found[number] for number in sorted(order)]


def last_attempt(events: Iterable[dict[str, Any]], reference: str = "") -> AttemptRecord | None:
    history = attempts(events, reference)
    return history[-1] if history else None
