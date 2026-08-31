"""What one completed worker or review phase cost, read from the provider's own records.

The dispatcher already knows which run served a phase: the routing snapshot holds the launch
configuration and the lifecycle run holds the bound provider source. This module is the only place
that turns those two into a token account, and it is deliberately the whole adapter-specific part
of the feature — everything downstream of it is one typed board event.

Two provider shapes, two aggregation rules, and the difference matters:

* Codex writes a ``token_count`` event whose ``total_token_usage`` is the session's running total.
  Summing those snapshots would multiply one phase's tokens by the number of turns, so the last
  well-formed snapshot *is* the phase total and a repeated or replayed snapshot costs nothing.
* Claude writes one ``usage`` object per assistant message, so the phase total is their sum. A
  streamed message is written more than once and a resumed session repeats earlier messages, so
  each message id contributes its last usage object exactly once.

Nothing here reads report or verdict prose, and nothing here decides anything: every failure is a
named degraded outcome, never an exception the caller has to survive.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from secretary.board.models import TOKEN_DIMENSIONS, AttemptUsageOutcome

CODEX_ADAPTER = "codex"
CLAUDE_ADAPTER = "claude"
CODEX_SOURCE_KIND = "codex_session_event_jsonl"
CLAUDE_SOURCE_KIND = "claude_session_jsonl"
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
    """One phase's account, with ``None`` for a dimension the provider does not report."""

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


@dataclass(frozen=True)
class UsageCollection:
    """The result of asking one provider source what a finished phase cost."""

    outcome: AttemptUsageOutcome
    detail: str = ""
    source_kind: str = ""
    # How many provider usage records the aggregation actually counted, and how many source lines
    # were unparseable. Both are evidence about the read, not about the phase.
    records: int = 0
    skipped_records: int = 0
    tokens: TokenTotals = field(default_factory=TokenTotals)

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


def collect_usage(*, adapter: str, source: Mapping[str, Any] | None) -> UsageCollection:
    """Read one finished phase's usage out of its bound provider source.

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
        records, skipped = read_jsonl(Path(path))
    except OSError as exc:
        return UsageCollection(
            AttemptUsageOutcome.SOURCE_UNREADABLE,
            detail=f"{adapter} session journal could not be read: {exc.strerror or exc}",
            source_kind=kind,
        )
    aggregate = codex_usage if adapter == CODEX_ADAPTER else claude_usage
    totals, counted = aggregate(records)
    if counted:
        return UsageCollection(
            AttemptUsageOutcome.COLLECTED,
            source_kind=kind,
            records=counted,
            skipped_records=skipped,
            tokens=totals,
        )
    if skipped and not records:
        return UsageCollection(
            AttemptUsageOutcome.RECORDS_MALFORMED,
            detail=f"{skipped} {adapter} session record(s) could not be parsed and none were usable",
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


def codex_usage(records: Iterable[Any]) -> tuple[TokenTotals, int]:
    """Aggregate Codex ``token_count`` events, whose totals are cumulative for the session.

    Returns the totals and how many snapshots were seen. The count is evidence about the read; the
    totals come from the last snapshot alone, so a journal replayed or re-read from its start
    reports the same account rather than a multiple of it.
    """
    latest: dict[str, Any] | None = None
    seen = 0
    for record in records:
        snapshot = _codex_snapshot(record)
        if snapshot is None:
            continue
        seen += 1
        latest = snapshot
    if latest is None:
        return TokenTotals(), 0
    return (
        TokenTotals(
            input=_count(latest, "input_tokens"),
            # Codex reports the cached share of its input and never a separate cache write, so the
            # cache-input dimension stays unavailable instead of being invented as zero.
            cache_input=None,
            cache_read_input=_count(latest, "cached_input_tokens"),
            output=_count(latest, "output_tokens"),
            reasoning=_count(latest, "reasoning_output_tokens"),
        ),
        seen,
    )


def claude_usage(records: Iterable[Any]) -> tuple[TokenTotals, int]:
    """Aggregate Claude assistant ``usage`` objects, which are per message and additive.

    Returns the totals and how many distinct messages were counted. A message id contributes once:
    its last usage object, because a streamed message is written repeatedly and only the final
    record carries the finished counts. A dimension no counted message reported stays unavailable.
    """
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, record in enumerate(records):
        usage = _claude_usage_object(record)
        if usage is None:
            continue
        key = _claude_message_key(record, index)
        if key not in latest:
            order.append(key)
        latest[key] = usage
    if not order:
        return TokenTotals(), 0
    sums: dict[str, int | None] = dict.fromkeys(CLAUDE_TOKEN_FIELDS, None)
    for key in order:
        usage = latest[key]
        for name, provider_field in CLAUDE_TOKEN_FIELDS.items():
            value = _count(usage, provider_field)
            if value is None:
                continue
            sums[name] = value if sums[name] is None else (sums[name] or 0) + value
    # Claude's session records expose no separate reasoning-token dimension.
    return TokenTotals(**sums, reasoning=None), len(order)


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
    }


def attempt_usage_reason(data: Mapping[str, Any]) -> str:
    """One line of prose for the journal's ``reason`` field, derived from the payload."""
    line = (
        f"attempt.usage: {data.get('role')} phase of attempt {data.get('attempt')} "
        f"(round {data.get('report_generation')}) on {data.get('adapter')}: {data.get('outcome')}"
    )
    detail = str(data.get("detail") or "")
    return f"{line} — {detail}" if detail else line


def _codex_snapshot(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    holder = info if isinstance(info, dict) else payload
    total = holder.get("total_token_usage")
    return dict(total) if isinstance(total, dict) else None


def _claude_usage_object(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    return dict(usage) if isinstance(usage, dict) else None


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
