"""Per-attempt routing telemetry: which head ran as worker and as reviewer on each attempt.

Contract: docs/PROTOCOLS.md, "Routing telemetry per attempt". The card's own metadata cannot answer
"who reviewed attempt 2": `resolved_review_head` is cleared when the card leaves Validate and the
whole routing block is reset on the way back to Ready, so the board keeps at most the last worker
head. The append-only task journal keeps every launch instead, one `routing` event per head
bring-up plus one per verdict, so a finished card still yields its worker/reviewer pairs.

A profile id alone is not a historical key: `codex`, `codex-terra`, `codex-high` and `codex-extra`
all resolve to one model with different effort, `claude-default` pins no model at all, and profiles
get re-pinned over time. So every event carries the launch configuration itself (adapter, model,
effort, codex launch mode, resource and account) snapshotted at bring-up, never re-read from
`heads.toml` afterwards.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from triggered_agents.agents.pipeline.heads import CODEX_TUI_MODE


ROUTING_KIND = "routing"
WORKER = "worker"
REVIEWER = "reviewer"
PHASES = ("worker", "review", "verdict")
# Where the head id itself came from.
HEAD_FROM_CARD = "card"
HEAD_FROM_ROLE_DEFAULT = "role_default"
HEAD_FROM_RECORD = "record"
# The claim walked the canon's fallback chain because the preferred head's resource was red or
# spent (secretary-1165). It is a source of its own rather than a flavour of `record`: "a weaker or
# simply other head did this work" is the one thing a reader of the journal must not have to infer.
HEAD_FROM_FALLBACK = "fallback"
# Where `model` was read at bring-up. `cli_default` means no configuration pinned a model and the
# adapter's CLI picked one itself at startup, which is the only case where `model` may be empty.
MODEL_FROM_PROFILE = "profile"
MODEL_FROM_CLI_DEFAULT = "cli_default"
MODEL_UNKNOWN = "unknown"
RUNTIME_MODEL_SOURCES = (MODEL_FROM_CLI_DEFAULT, MODEL_UNKNOWN)


@dataclass(frozen=True)
class HeadRun:
    """One head as it was actually launched.

    There is one head per role per bring-up and no substitution between the decision and the
    launch: `head` is what started, and it is what the claim decided. `head_source` says where that
    id came from: the card's own override, the role default, the dispatcher record of a card
    claimed earlier, or the canon's fallback chain when the claim had to walk it because the
    preferred head's resource was red or spent.

    `model` may be empty only under a `model_source` that says the CLI resolved it at startup, so a
    profile that pins no model (`claude-default`) can never be recorded as a silent blank.
    """

    role: str
    head: str
    head_source: str = HEAD_FROM_ROLE_DEFAULT
    adapter: str = ""
    model: str = ""
    model_source: str = MODEL_UNKNOWN
    effort: str = ""
    codex_mode: str = ""
    resource: str = ""
    account: str = ""

    def __post_init__(self) -> None:
        if not self.model_source:
            raise ValueError("head run must say where its model came from")
        if not self.model and self.model_source not in RUNTIME_MODEL_SOURCES:
            raise ValueError(
                f"head run {self.head!r} has no model under source {self.model_source!r}; "
                f"an unpinned model must be recorded as one of {', '.join(RUNTIME_MODEL_SOURCES)}"
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "head": self.head,
            "head_source": self.head_source,
            "adapter": self.adapter,
            "model": self.model,
            "model_source": self.model_source,
            "effort": self.effort,
            "codex_mode": self.codex_mode,
            "resource": self.resource,
            "account": self.account,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "HeadRun":
        model = str(payload.get("model") or "")
        source = str(payload.get("model_source") or "")
        if not source or (not model and source not in RUNTIME_MODEL_SOURCES):
            # A record written before this contract, or a hand-edited one. Reading must not fail on
            # it, but it must not read as a known model either.
            source = MODEL_UNKNOWN if not model else MODEL_FROM_PROFILE
        return cls(
            role=str(payload.get("role") or ""),
            head=str(payload.get("head") or ""),
            head_source=str(payload.get("head_source") or HEAD_FROM_ROLE_DEFAULT),
            adapter=str(payload.get("adapter") or ""),
            model=model,
            model_source=source,
            effort=str(payload.get("effort") or ""),
            codex_mode=str(payload.get("codex_mode") or ""),
            resource=str(payload.get("resource") or ""),
            account=str(payload.get("account") or ""),
        )


def head_run_from_profile(
    *,
    role: str,
    head: str,
    head_source: str,
    profile: dict[str, Any],
    resources: dict[str, Any],
    model: str | None = None,
    model_source: str = "",
) -> HeadRun:
    """Snapshot the profile that was launched.

    A Codex head is recorded under the one launch mode the product has, and it is written here
    rather than copied from the profile or from the card: `codex_launch_mode` on a card is retired
    routing data that selects nothing, and a legacy `exec` in either place would put a mode in the
    journal that no head of this bring-up could have run in.

    `model` overrides the profile's own field for a head whose model the profile does not decide: a
    claude profile without `model` renders a command without `--model` and the CLI resolves one at
    startup, so the caller passes what it will resolve to, with `model_source` naming where it read
    it. Without an override the profile is the source, and a profile that pins nothing is recorded
    as resolved by the CLI rather than as an empty field.
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
        mode = CODEX_TUI_MODE
        effort = str(profile.get("effort") or "default")
    elif adapter == "claude":
        effort = str(profile.get("effort") or "default")
    if model is None:
        model = str(profile.get("model") or "")
        model_source = model_source or (MODEL_FROM_PROFILE if model else MODEL_FROM_CLI_DEFAULT)
    return HeadRun(
        role=role,
        head=head,
        head_source=head_source,
        adapter=adapter,
        model=str(model),
        model_source=model_source or (MODEL_FROM_PROFILE if model else MODEL_FROM_CLI_DEFAULT),
        effort=effort,
        codex_mode=mode,
        resource=resource,
        account=account,
    )


def run_key(run: HeadRun | dict[str, Any] | None) -> str:
    """Short digest of one launch configuration.

    A round can bring the same role up more than once (a respawn after a silent head, a recovery
    restart, a rework relaunch), and the relaunched head is not necessarily configured like the one
    that started the round: a `heads.toml` repin lands a different model or effort. The digest is
    what tells "the same head came back" from "a different configuration now serves this round", so
    the journal can stay idempotent on the former and still append an event for the latter.
    """
    payload = run.to_json() if isinstance(run, HeadRun) else dict(run or {})
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
    # whose reviewer was relaunched onto a repinned profile keeps both records, and `reviewer` is
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
