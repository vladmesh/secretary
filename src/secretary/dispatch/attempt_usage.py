"""What one completed worker or review phase cost, read from the provider's own records.

The dispatcher already knows which run served a phase: the routing snapshot holds the launch
configuration and the lifecycle run holds the bound provider source. This module is the only place
that turns those two into a token account, and it is deliberately the whole adapter-specific part
of the feature — everything downstream of it is one typed board event.

Both providers write a journal that describes a *session*, not a phase, so the module reads a
session total and then subtracts the boundary the previous phase on that same session already made
durable. One rule for both adapters: a phase owns the usage after the previous authoritative terminal
boundary for its provider session and through its own terminal boundary, and a session nobody has
accounted for yet starts at zero. That is what keeps a retained worker — the same conversation
resumed for a second round — from being charged twice for its first round.

The two session shapes still differ in how the running total is obtained, and the difference matters:

* Codex writes cumulative ``token_count`` snapshots, but can reset those counters after a new user
  turn without changing the session or journal. The session total is the sum of each reset segment's
  last snapshot. Repeated snapshots within a segment still cost nothing.
* Claude writes one ``usage`` object per assistant message, so the session total is their sum. A
  streamed message is written more than once and a resumed session repeats earlier messages, so
  each message id contributes its last usage object exactly once.

Nothing here reads report or verdict prose, and nothing here decides anything: every failure is a
named degraded outcome, never an exception the caller has to survive.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from secretary.board.events import AttemptUsageOccurrence
from secretary.board.models import TOKEN_DIMENSIONS, AttemptUsageOutcome, EventKind

CODEX_ADAPTER = "codex"
CLAUDE_ADAPTER = "claude"
CODEX_SOURCE_KIND = "codex_session_event_jsonl"
CLAUDE_SOURCE_KIND = "claude_session_jsonl"
ATTEMPT_USAGE_KIND = EventKind.ATTEMPT_USAGE.value
# Which fan-out policy key holds the structured record source for each adapter. The same split the
# routing journal uses to resolve a session id, so both read one run the same way.
SOURCE_POLICY_KEYS = {
    CODEX_ADAPTER: "provider_source",
    CLAUDE_ADAPTER: "provider_progress_source",
}
SOURCE_KINDS = {
    CODEX_ADAPTER: CODEX_SOURCE_KIND,
    CLAUDE_ADAPTER: CLAUDE_SOURCE_KIND,
}
# The Codex snapshot's own spelling of the dimensions it reports. Its input and output totals include
# their cache and reasoning subcounts, respectively; ``_canonical_codex_totals`` removes those
# contained counts so every exported bucket is non-overlapping.
CODEX_TOKEN_FIELDS = {
    "input": "input_tokens",
    "cache_input": "cache_write_input_tokens",
    "cache_read_input": "cached_input_tokens",
    "output": "output_tokens",
    "reasoning": "reasoning_output_tokens",
}
# The Claude usage object's own spelling of the dimensions it reports. Reasoning tokens are absent
# from it, and stay unavailable rather than being folded into output.
CLAUDE_TOKEN_FIELDS = {
    "input": "input_tokens",
    "cache_input": "cache_creation_input_tokens",
    "cache_read_input": "cache_read_input_tokens",
    "output": "output_tokens",
}


@dataclass(frozen=True)
class TokenTotals:
    """One account, with ``None`` for a dimension the provider does not report."""

    input: int | None = None
    cache_input: int | None = None
    cache_read_input: int | None = None
    output: int | None = None
    reasoning: int | None = None

    def to_json(self) -> dict[str, int | None]:
        return {name: getattr(self, name) for name in TOKEN_DIMENSIONS}

    @property
    def empty(self) -> bool:
        return all(getattr(self, name) is None for name in TOKEN_DIMENSIONS)

    @classmethod
    def from_json(cls, source: Mapping[str, Any] | None) -> TokenTotals | None:
        """One recorded account, or ``None`` when the record carries no usable dimension."""
        if not isinstance(source, Mapping):
            return None
        values = {name: _count(source, name) for name in TOKEN_DIMENSIONS}
        if all(value is None for value in values.values()):
            return None
        return cls(**values)


@dataclass(frozen=True)
class SessionUsage:
    """What a provider journal says about the whole session it describes."""

    totals: TokenTotals = field(default_factory=TokenTotals)
    # How many usable structured usage records the aggregation counted.
    records: int = 0
    # How many records declared themselves usage records and carried no usable schema.
    invalid: int = 0


@dataclass(frozen=True)
class UsageCollection:
    """The result of asking one provider source what a finished phase cost."""

    outcome: AttemptUsageOutcome
    detail: str = ""
    source_kind: str = ""
    # How many provider usage records the aggregation actually counted, and how many source records
    # were unusable. Both are evidence about the read, not about the phase.
    records: int = 0
    skipped_records: int = 0
    # What the phase owns: the session total at its terminal boundary minus the boundary the
    # previous phase on this session left behind.
    tokens: TokenTotals = field(default_factory=TokenTotals)
    # The session total this phase ends at, which is the next phase's starting boundary.
    session_totals: TokenTotals = field(default_factory=TokenTotals)
    # The boundary this phase started from: zero for a session nobody has accounted for yet.
    baseline: TokenTotals = field(default_factory=TokenTotals)

    @property
    def collected(self) -> bool:
        return self.outcome is AttemptUsageOutcome.COLLECTED


def provider_usage_source(lifecycle_run: Mapping[str, Any] | None, *, adapter: str) -> dict[str, Any]:
    """The adapter's structured record source held by one lifecycle run, or an empty mapping."""
    policy = lifecycle_run.get("fanout_policy") if isinstance(lifecycle_run, Mapping) else None
    if not isinstance(policy, Mapping):
        return {}
    source = policy.get(SOURCE_POLICY_KEYS.get(adapter, ""))
    return dict(source) if isinstance(source, Mapping) else {}


def causal_phase_boundary(
    occurrences: Sequence[AttemptUsageOccurrence],
    *,
    adapter: str,
    session_id: str,
    attempt: int,
    attempt_id: str,
    report_generation: int,
    phase: str,
    role: str,
) -> TokenTotals | None:
    """The causal predecessor's boundary for this provider session.

    Committed and staged occurrences have equal semantic authority. Their append order is irrelevant:
    the phase identity carried by each event decides which occurrence precedes this one. A matching
    predecessor without a usable boundary is an audit failure, not permission to restart at zero.
    """
    if not session_id:
        return None
    phase_order = {"worker": 0, "review": 1}
    current = (attempt, report_generation, phase_order[phase])
    predecessors: list[tuple[tuple[int, int, int], AttemptUsageOccurrence]] = []
    for occurrence in occurrences:
        data = occurrence.event.data
        if data["role"] != role or data["phase"] != phase:
            continue
        order = (
            int(data["attempt"]),
            int(data["report_generation"]),
            phase_order[str(data["phase"])],
        )
        if order == current and data["attempt_id"] != attempt_id:
            raise ValueError("current attempt usage phase belongs to a conflicting attempt id")
        if order < current and data["adapter"] == adapter and data["session_id"] == session_id:
            predecessors.append((order, occurrence))
    if not predecessors:
        return None
    _order, predecessor = max(predecessors, key=lambda item: item[0])
    boundary = TokenTotals.from_json(predecessor.event.data.get("session_totals"))
    if boundary is None:
        identity = predecessor.event.data
        raise ValueError(
            "causal attempt usage predecessor has no readable session-total boundary "
            f"(attempt {identity['attempt']}, generation {identity['report_generation']})"
        )
    return boundary


def phase_interval(
    session_totals: TokenTotals,
    baseline: TokenTotals | None,
) -> tuple[TokenTotals, TokenTotals, bool]:
    """Split a session total into the interval this phase owns and the boundary it started from.

    Returns ``(phase, baseline, rewound)``. ``rewound`` says the session total came back lower than
    a boundary already made durable. The tentative interval contains no negative number; the caller
    turns the contradiction into a typed degraded outcome rather than publishing this interval.
    """
    phase: dict[str, int | None] = {}
    start: dict[str, int | None] = {}
    rewound = False
    for name in TOKEN_DIMENSIONS:
        current = getattr(session_totals, name)
        if current is None:
            phase[name] = start[name] = None
            continue
        previous = getattr(baseline, name) if baseline is not None else None
        previous = 0 if previous is None else previous
        start[name] = previous
        if current < previous:
            rewound = True
            phase[name] = 0
            continue
        phase[name] = current - previous
    return TokenTotals(**phase), TokenTotals(**start), rewound


def apply_phase_boundary(
    collection: UsageCollection,
    baseline: TokenTotals | None,
) -> UsageCollection:
    """Apply one projected predecessor to an already-read provider session total.

    Keeping this separate from the provider read preserves failure precedence without rereading the
    provider journal: a degraded current read needs no arithmetic, while a collected one must have
    its causal predecessor resolved before it becomes the phase occurrence.
    """
    if not collection.collected:
        return collection
    tokens, start, rewound = phase_interval(collection.session_totals, baseline)
    if rewound:
        return replace(
            collection,
            outcome=AttemptUsageOutcome.ARITHMETIC_CONTRADICTION,
            detail=(
                "the provider session total fell below the authoritative boundary of an earlier phase"
            ),
            tokens=TokenTotals(),
            session_totals=TokenTotals(),
            baseline=TokenTotals(),
        )
    return replace(
        collection,
        detail=collection.detail,
        tokens=tokens,
        baseline=start,
    )


def collect_usage(
    *,
    adapter: str,
    source: Mapping[str, Any] | None,
    baseline: TokenTotals | None = None,
) -> UsageCollection:
    """Read one finished phase's usage out of its bound provider source.

    ``baseline`` is the previous authoritative boundary for this same provider session, so what comes back
    is the interval this phase owns rather than everything the session has ever spent.

    Never raises: every way this can fail is one of the declared degraded outcomes, because the
    caller runs on the path that accepts a worker report or a reviewer verdict.
    """
    kind = SOURCE_KINDS.get(adapter, "")
    if not kind:
        return UsageCollection(
            AttemptUsageOutcome.ADAPTER_UNSUPPORTED,
            detail=f"adapter {adapter or 'unknown'} publishes no structured usage records",
        )
    source = dict(source or {})
    state = str(source.get("state") or "")
    if str(source.get("kind") or "") != kind or state != "bound":
        return UsageCollection(
            AttemptUsageOutcome.SOURCE_UNAVAILABLE,
            detail=f"{adapter} structured record source is {state or 'absent'} for this run",
            source_kind=kind,
        )
    if not str(source.get("session_id") or ""):
        return UsageCollection(
            AttemptUsageOutcome.SESSION_UNAVAILABLE,
            detail=f"{adapter} bound source carries no provider session id",
            source_kind=kind,
        )
    path = str(source.get("path") or "")
    if not path:
        return UsageCollection(
            AttemptUsageOutcome.SOURCE_UNAVAILABLE,
            detail=f"{adapter} bound source names no journal path",
            source_kind=kind,
        )
    try:
        records, unparsed = read_jsonl(Path(path))
    except OSError as exc:
        return UsageCollection(
            AttemptUsageOutcome.SOURCE_UNREADABLE,
            detail=f"{adapter} session journal could not be read: {exc.strerror or exc}",
            source_kind=kind,
        )
    aggregate = codex_usage if adapter == CODEX_ADAPTER else claude_usage
    session = aggregate(records)
    skipped = unparsed + session.invalid
    if session.records:
        return apply_phase_boundary(
            UsageCollection(
                AttemptUsageOutcome.COLLECTED,
                source_kind=kind,
                records=session.records,
                skipped_records=skipped,
                session_totals=session.totals,
            ),
            baseline,
        )
    if session.invalid:
        return UsageCollection(
            AttemptUsageOutcome.RECORDS_MALFORMED,
            detail=(
                f"{session.invalid} {adapter} record(s) declare a usage record whose schema is not "
                "the one this adapter publishes, and none were usable"
            ),
            source_kind=kind,
            skipped_records=skipped,
        )
    if unparsed and not records:
        return UsageCollection(
            AttemptUsageOutcome.RECORDS_MALFORMED,
            detail=f"{unparsed} {adapter} session record(s) could not be parsed and none were usable",
            source_kind=kind,
            skipped_records=skipped,
        )
    return UsageCollection(
        AttemptUsageOutcome.USAGE_ABSENT,
        detail=f"{adapter} session journal carries no usage record for this phase",
        source_kind=kind,
        skipped_records=skipped,
    )


def read_jsonl(path: Path) -> tuple[list[Any], int]:
    """Parse one provider JSONL file, returning its records and how many lines would not parse.

    A truncated final line — the normal shape of a journal read while its writer is still around —
    is one skipped line, not a failed read: the records before it are complete records.
    """
    records: list[Any] = []
    skipped = 0
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                skipped += 1
    return records, skipped


def codex_usage(records: Iterable[Any]) -> SessionUsage:
    """Aggregate cumulative Codex snapshots into a monotone whole-session total.

    Counters are cumulative only until Codex starts a new reset segment, which can happen after a
    new user turn in the same session and rollout. A decrease in any reported raw counter closes the
    prior segment; summing segment endpoints produces a monotone session total. Equal/replayed
    snapshots remain in one segment. The canonical buckets are derived at each endpoint because
    Codex's raw input includes cache counts and raw output includes reasoning counts.
    """
    latest: dict[str, Any] | None = None
    completed: list[TokenTotals] = []
    seen = 0
    invalid = 0
    for record in records:
        snapshot, malformed = _codex_snapshot(record)
        if snapshot is None:
            invalid += malformed
            continue
        canonical = _canonical_codex_totals(snapshot)
        if canonical is None:
            invalid += 1
            continue
        if latest is not None and _codex_snapshot_reset(latest, snapshot):
            endpoint = _canonical_codex_totals(latest)
            if endpoint is None:  # Every assigned ``latest`` was validated above.
                invalid += 1
            else:
                completed.append(endpoint)
        seen += 1
        latest = snapshot
    if latest is None:
        return SessionUsage(invalid=invalid)
    endpoint = _canonical_codex_totals(latest)
    if endpoint is None:
        return SessionUsage(invalid=invalid + 1)
    completed.append(endpoint)
    return SessionUsage(
        totals=_sum_token_totals(completed),
        records=seen,
        invalid=invalid,
    )


def claude_usage(records: Iterable[Any]) -> SessionUsage:
    """Aggregate Claude assistant ``usage`` objects, which are per message and additive.

    A message id contributes once: its last usage object, because a streamed message is written
    repeatedly and only the final record carries the finished counts. A dimension no counted message
    reported stays unavailable, and an assistant record whose declared ``usage`` is not the object
    this adapter publishes is a malformed structured record rather than a message that cost nothing.
    """
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    invalid = 0
    for index, record in enumerate(records):
        usage, malformed = _claude_usage_object(record)
        if usage is None:
            invalid += malformed
            continue
        key = _claude_message_key(record, index)
        if key not in latest:
            order.append(key)
        latest[key] = usage
    if not order:
        return SessionUsage(invalid=invalid)
    sums: dict[str, int | None] = dict.fromkeys(CLAUDE_TOKEN_FIELDS, None)
    for key in order:
        usage = latest[key]
        for name, provider_field in CLAUDE_TOKEN_FIELDS.items():
            value = _count(usage, provider_field)
            if value is None:
                continue
            sums[name] = value if sums[name] is None else (sums[name] or 0) + value
    # Claude's session records expose no separate reasoning-token dimension.
    return SessionUsage(totals=TokenTotals(**sums, reasoning=None), records=len(order), invalid=invalid)


def attempt_usage_data(
    *,
    attempt: int,
    attempt_id: str,
    phase: str,
    role: str,
    report_generation: int,
    head: str,
    adapter: str,
    model: str,
    model_source: str,
    session_id: str | None,
    session_id_reason: str,
    launch_id: str,
    collection: UsageCollection,
) -> dict[str, Any]:
    """The complete, self-contained ``attempt.usage`` payload for one finished phase."""
    return {
        "attempt": int(attempt),
        "attempt_id": attempt_id,
        "phase": phase,
        "role": role,
        "report_generation": int(report_generation),
        "head": head,
        "adapter": adapter,
        "model": model,
        "model_source": model_source,
        "session_id": session_id or None,
        "session_id_reason": session_id_reason,
        "launch_id": launch_id,
        "outcome": AttemptUsageOutcome(collection.outcome).value,
        "detail": collection.detail,
        "source_kind": collection.source_kind,
        "records": int(collection.records),
        "skipped_records": int(collection.skipped_records),
        "tokens": collection.tokens.to_json(),
        "session_totals": collection.session_totals.to_json(),
        "phase_baseline": collection.baseline.to_json(),
    }


def attempt_usage_reason(data: Mapping[str, Any]) -> str:
    """One line of prose for the journal's ``reason`` field, derived from the payload."""
    line = (
        f"attempt.usage: {data.get('role')} phase of attempt {data.get('attempt')} "
        f"(round {data.get('report_generation')}) on {data.get('adapter')}: {data.get('outcome')}"
    )
    detail = str(data.get("detail") or "")
    return f"{line} — {detail}" if detail else line


def _codex_snapshot(record: Any) -> tuple[dict[str, Any] | None, bool]:
    """One session total, and whether the record declared a total it did not actually carry."""
    if not isinstance(record, dict):
        return None, False
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None, False
    info = payload.get("info")
    holder = info if isinstance(info, dict) else payload
    total = holder.get("total_token_usage") if isinstance(holder, dict) else None
    if not isinstance(total, dict):
        return None, True
    if not any(_count(total, field) is not None for field in CODEX_TOKEN_FIELDS.values()):
        return None, True
    return dict(total), False


def _canonical_codex_totals(snapshot: Mapping[str, Any]) -> TokenTotals | None:
    """Convert one Codex endpoint to the five non-overlapping protocol buckets.

    Codex defines both cache counts inside ``input_tokens`` and reasoning inside ``output_tokens``.
    All five raw fields must be present to make the canonical account self-sufficient. Impossible
    containment is a malformed provider snapshot rather than a negative or silently clamped bucket.
    """
    raw = {name: _count(snapshot, field) for name, field in CODEX_TOKEN_FIELDS.items()}
    if any(value is None for value in raw.values()):
        return None
    values = {name: int(value) for name, value in raw.items()}
    uncached = values["input"] - values["cache_input"] - values["cache_read_input"]
    non_reasoning = values["output"] - values["reasoning"]
    if uncached < 0 or non_reasoning < 0:
        return None
    return TokenTotals(
        input=uncached,
        cache_input=values["cache_input"],
        cache_read_input=values["cache_read_input"],
        output=non_reasoning,
        reasoning=values["reasoning"],
    )


def _codex_snapshot_reset(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Whether a later usable snapshot starts a new cumulative-counter segment."""
    return any(
        current_value < previous_value
        for field in CODEX_TOKEN_FIELDS.values()
        if (previous_value := _count(previous, field)) is not None
        and (current_value := _count(current, field)) is not None
    )


def _sum_token_totals(parts: Sequence[TokenTotals]) -> TokenTotals:
    """Sum segment endpoints, keeping a dimension unavailable if any segment lacks it."""
    totals: dict[str, int | None] = {}
    for name in TOKEN_DIMENSIONS:
        values = [getattr(part, name) for part in parts]
        totals[name] = (
            None if any(value is None for value in values) else sum(value for value in values if value is not None)
        )
    return TokenTotals(**totals)


def _claude_usage_object(record: Any) -> tuple[dict[str, Any] | None, bool]:
    """One message's usage object, and whether the record declared one it did not actually carry."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None, False
    message = record.get("message")
    if not isinstance(message, dict) or "usage" not in message:
        return None, False
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None, True
    if not any(_count(usage, field) is not None for field in CLAUDE_TOKEN_FIELDS.values()):
        return None, True
    return dict(usage), False


def _claude_message_key(record: Mapping[str, Any], index: int) -> str:
    """The occurrence key of one assistant record: its message, else the record, else its line."""
    message = record.get("message")
    identifier = str(message.get("id") or "") if isinstance(message, Mapping) else ""
    if identifier:
        return f"message:{identifier}"
    record_id = str(record.get("uuid") or "")
    if record_id:
        return f"record:{record_id}"
    return f"line:{index}"


def _count(source: Mapping[str, Any], name: str) -> int | None:
    """One provider count, or ``None`` when it is absent or is not a usable non-negative integer."""
    value = source.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
