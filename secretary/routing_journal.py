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

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable


ROUTING_KIND = "routing"
WORKER = "worker"
REVIEWER = "reviewer"
PHASES = ("worker", "review", "verdict")
# Where the head that really launched came from, relative to what the card asked for.
FROM_REQUESTED = "requested"
FROM_RETRY_SWITCH = "retry_switch"
FROM_LAUNCH = "launch"


@dataclass(frozen=True)
class HeadRun:
    """One head as it was actually launched.

    `requested` is the profile the card (or the role default) asked for, `resolved` is the profile
    the launcher was actually handed. They differ exactly when a fallback fired, which `fallback`
    states outright so a later diversity analysis never has to infer it from a chain in a
    `heads.toml` that has since moved on. `resolved_from` says which road produced the difference:
    the card's own watchdog head-switch history (`retry_switch`) or a bring-up that launched
    something other than what the card asks for right now (`launch`).
    """

    role: str
    requested: str
    resolved: str
    requested_from: str = "role_default"
    resolved_from: str = FROM_REQUESTED
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
            "resolved_from": self.resolved_from,
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
            resolved_from=str(payload.get("resolved_from") or FROM_REQUESTED),
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
    resolved_from: str = "",
    fallback_chain: Iterable[str] | None = None,
) -> HeadRun:
    """Snapshot the profile that was launched. `codex_mode` overrides the profile's own launch mode
    (a card can pin `codex_launch_mode`), so the record shows the mode the head really started in.

    `fallback_chain` is the chain declared by the *requested* profile — the policy in force at this
    bring-up — and defaults to the launched profile's own chain when the caller has no better
    source.
    """
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
    chain = profile.get("fallback") if fallback_chain is None else fallback_chain
    return HeadRun(
        role=role,
        requested=requested,
        resolved=resolved,
        requested_from=requested_from,
        resolved_from=resolved_from or (FROM_REQUESTED if resolved == requested else FROM_LAUNCH),
        adapter=adapter,
        model=str(profile.get("model") or ""),
        effort=effort,
        codex_mode=mode,
        resource=resource,
        account=account,
        fallback_chain=tuple(str(item) for item in chain) if isinstance(chain, (list, tuple)) else (),
    )


def run_key(run: HeadRun | dict[str, Any] | None) -> str:
    """Short digest of one launch configuration.

    A round can bring the same role up more than once — a respawn after a silent head, a recovery
    restart, a rework relaunch — and the relaunched head is not necessarily the one that started
    the round: a `heads.toml` repin or a switch lands a different adapter/model/resource. The
    digest is what tells "the same head came back" from "a different head now serves this round",
    so the journal can stay idempotent on the former and still append an event for the latter.
    """
    payload = run.to_json() if isinstance(run, HeadRun) else dict(run or {})
    payload.pop("fallback_chain", None)
    material = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


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
    # Every bring-up of the round in journal order, not just the head that served it last: a round
    # whose reviewer was relaunched onto a different model keeps both records, and `reviewer` is
    # the one the verdict came from.
    worker_runs: list[HeadRun] = field(default_factory=list)
    reviewer_runs: list[HeadRun] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "attempt_id": self.attempt_id,
            "worker": self.worker.to_json() if self.worker else None,
            "reviewer": self.reviewer.to_json() if self.reviewer else None,
            "worker_runs": [run.to_json() for run in self.worker_runs],
            "reviewer_runs": [run.to_json() for run in self.reviewer_runs],
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
        phase = str(payload.get("phase") or "")
        record.events.append(phase)
        for head in payload.get("heads") or []:
            if not isinstance(head, dict):
                continue
            run = HeadRun.from_json(head)
            if run.role == WORKER:
                record.worker = run
                if phase == "worker":
                    record.worker_runs.append(run)
            elif run.role == REVIEWER:
                record.reviewer = run
                if phase == "review":
                    record.reviewer_runs.append(run)
        outcome = str(payload.get("outcome") or "")
        if outcome:
            record.outcome = outcome
    return [found[number] for number in sorted(order)]


def last_attempt(events: Iterable[dict[str, Any]], reference: str = "") -> AttemptRecord | None:
    history = attempts(events, reference)
    return history[-1] if history else None
