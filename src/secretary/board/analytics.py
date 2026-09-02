"""Offline, verifier-first analytics projection for sealed board checkpoints.

This module deliberately accepts a copied ``state/board`` directory, not an
installation.  It has no control-plane dependencies: after the manifest
verifier seals the cut, it reads the checkpoint's NDJSON files and joins an
outcome to usage only through the outcome's explicit event ids.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.board.models import EntityKind, Event, EventKind
from secretary.checkpoint import AnalyticsCheckpoint, verify_analytics_checkpoint

ANALYTICS_PROJECTION_VERSION = 1
_CHECKPOINT_ROWS = ("cards.ndjson", "sprints.ndjson", "events.ndjson")
_NON_USAGE_SOURCE_KINDS = {
    "report": EventKind.CARD_REPORTED,
    "verdict": EventKind.CARD_VERDICTED,
    "decision": EventKind.CARD_DECIDED,
}


class AnalyticsProjectionError(ValueError):
    """A named, fail-closed diagnostic for an untrustworthy analytics cut."""

    def __init__(self, code: str, path: Path, record: int | None, detail: str) -> None:
        self.code = code
        self.path = path
        self.record = record
        self.detail = detail
        location = str(path)
        if record is not None:
            location += f" record {record}"
        super().__init__(f"{code}: {location}: {detail}")


@dataclass(frozen=True, slots=True)
class AnalyticsProjection:
    """Versioned, NDJSON-ready rows and explicit evidence incompleteness."""

    version: int
    checkpoint_id: str
    rows: tuple[dict[str, Any], ...]
    incomplete: bool
    incomplete_reasons: tuple[str, ...]

    def ndjson(self) -> str:
        """Render the stable row objects without assigning any new identity."""
        return "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in self.rows)


@dataclass(frozen=True, slots=True)
class _RecordedEvent:
    event: Event
    request_id: str
    path: Path
    number: int
    record: dict[str, Any]


def project_analytics_checkpoint(directory: str | Path) -> AnalyticsProjection:
    """Project one sealed checkpoint without reading live board or provider state.

    ``verify_analytics_checkpoint`` is intentionally the first operation.  A
    failed manifest therefore prevents any analytics JSON parsing.  The only
    later reads are the declared checkpoint rows below its verified directory;
    ``export.json`` remains a verifier-checked summary, never row evidence.
    """
    checkpoint = verify_analytics_checkpoint(Path(directory))
    cards = _references(checkpoint, "cards.ndjson")
    sprints = _references(checkpoint, "sprints.ndjson")
    events = _events(checkpoint)
    _validate_subject_references(events, cards, sprints)
    rows = _outcome_rows(checkpoint, events, cards, sprints)
    rows.sort(key=lambda row: (row["card_ref"], row["attempt_id"], row["report_generation"]))
    reasons = _incomplete_reasons(rows)
    return AnalyticsProjection(
        version=ANALYTICS_PROJECTION_VERSION,
        checkpoint_id=checkpoint.checkpoint_id,
        rows=tuple(rows),
        incomplete=bool(reasons),
        incomplete_reasons=tuple(reasons),
    )


def _read_ndjson(checkpoint: AnalyticsCheckpoint, name: str) -> list[tuple[int, dict[str, Any]]]:
    if name not in _CHECKPOINT_ROWS:
        raise AssertionError(f"undeclared analytics row file {name}")
    path = checkpoint.directory / name
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AnalyticsProjectionError("analytics_read_failed", path, None, str(exc)) from None
    rows: list[tuple[int, dict[str, Any]]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise AnalyticsProjectionError("analytics_malformed_row", path, number, str(exc)) from None
        if not isinstance(value, dict):
            raise AnalyticsProjectionError("analytics_malformed_row", path, number, "row must be an object")
        rows.append((number, value))
    return rows


def _references(checkpoint: AnalyticsCheckpoint, name: str) -> set[str]:
    references: set[str] = set()
    for _, row in _read_ndjson(checkpoint, name):
        reference = row.get("reference")
        # Board exports retain archived rows.  The projection only needs to
        # establish that an event subject belongs to the cut, so this is a
        # membership collection, not an identity table.  Ignore rows that do
        # not contribute a usable reference, including unrelated board rows.
        if isinstance(reference, str) and reference.strip():
            references.add(reference)
    return references


def _events(checkpoint: AnalyticsCheckpoint) -> dict[str, _RecordedEvent]:
    path = checkpoint.directory / "events.ndjson"
    events: dict[str, _RecordedEvent] = {}
    requests: dict[str, _RecordedEvent] = {}
    for number, record in _read_ndjson(checkpoint, "events.ndjson"):
        if record.get("record_type") != Event.RECORD_TYPE:
            continue
        try:
            event = Event.from_record(record)
        except ValueError as exc:
            raise AnalyticsProjectionError("analytics_invalid_typed_event", path, number, str(exc)) from None
        request_id = record["request_id"]
        candidate = _RecordedEvent(event, request_id, path, number, record)
        previous_event = events.get(event.event_id)
        if previous_event is not None:
            if previous_event.request_id != request_id or previous_event.record != record:
                raise AnalyticsProjectionError(
                    "analytics_conflicting_event_identity",
                    path,
                    number,
                    f"event id {event.event_id!r} conflicts with record {previous_event.number}",
                )
            continue
        previous_request = requests.get(request_id)
        if previous_request is not None:
            if previous_request.event.event_id != event.event_id or previous_request.record != record:
                raise AnalyticsProjectionError(
                    "analytics_conflicting_request_ownership",
                    path,
                    number,
                    f"request id {request_id!r} conflicts with record {previous_request.number}",
                )
            continue
        events[event.event_id] = candidate
        requests[request_id] = candidate
    return events


def _validate_subject_references(
    events: dict[str, _RecordedEvent], cards: set[str], sprints: set[str]
) -> None:
    for recorded in events.values():
        event = recorded.event
        if event.entity_kind is EntityKind.CARD and event.ref not in cards:
            _fail(
                "analytics_dangling_card_ref",
                recorded,
                f"Card subject {event.ref!r} is absent from cards.ndjson",
            )
        if event.entity_kind is EntityKind.SPRINT and event.ref not in sprints:
            _fail(
                "analytics_dangling_sprint_ref",
                recorded,
                f"Sprint subject {event.ref!r} is absent from sprints.ndjson",
            )


def _outcome_rows(
    checkpoint: AnalyticsCheckpoint,
    events: dict[str, _RecordedEvent],
    cards: set[str],
    sprints: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    natural_keys: dict[tuple[str, str, int], _RecordedEvent] = {}
    for recorded in events.values():
        outcome = recorded.event
        if outcome.kind is not EventKind.ATTEMPT_OUTCOME:
            continue
        data = outcome.data
        key = (outcome.ref, str(data["attempt_id"]), int(data["report_generation"]))
        existing = natural_keys.get(key)
        if existing is not None:
            _fail(
                "analytics_conflicting_outcome_natural_key",
                recorded,
                f"outcome key {key!r} conflicts with record {existing.number}",
            )
        natural_keys[key] = recorded
        sprint_ref = data["sprint_ref"]
        if sprint_ref is not None and sprint_ref not in sprints:
            _fail("analytics_dangling_sprint_ref", recorded, f"outcome sprint {sprint_ref!r} is absent")
        # Event.from_record already establishes the event's Card subject. Keep
        # this local assertion so a future typed-boundary change cannot turn a
        # projection join into a live-card lookup.
        if outcome.ref not in cards:
            _fail("analytics_dangling_card_ref", recorded, f"outcome card {outcome.ref!r} is absent")
        refs = data["source_event_ids"]
        _validate_non_usage_sources(recorded, refs, events)
        worker = _usage_source(recorded, "worker", refs["worker_usage"], events)
        review = _usage_source(recorded, "reviewer", refs["review_usage"], events)
        lineage = _lineage_completeness(data)
        rows.append(
            {
                "projection_version": ANALYTICS_PROJECTION_VERSION,
                "checkpoint_id": checkpoint.checkpoint_id,
                "card_ref": outcome.ref,
                "attempt_id": data["attempt_id"],
                "attempt": data["attempt"],
                "report_generation": data["report_generation"],
                "sprint_ref": sprint_ref,
                "specification_revision": data["specification_revision"],
                "terminal_state": data["terminal_state"],
                "verdict": data["verdict"],
                "disposition": data["disposition"],
                "blocked_reason": data["blocked_reason"],
                "source_event_ids": dict(refs),
                "usage_completeness": dict(data["usage_completeness"]),
                "lineage_completeness": lineage,
                "worker_usage": worker,
                "review_usage": review,
            }
        )
        _validate_usage_completeness(recorded, data["usage_completeness"], worker, review)
    return rows


def _validate_non_usage_sources(
    outcome: _RecordedEvent, refs: dict[str, Any], events: dict[str, _RecordedEvent]
) -> None:
    for name, expected_kind in _NON_USAGE_SOURCE_KINDS.items():
        event_id = refs[name]
        if event_id is None:
            continue
        source = events.get(event_id)
        if source is None:
            _fail(
                "analytics_dangling_source_event_ref", outcome, f"{name} refers to absent event {event_id!r}"
            )
        if source.event.kind is not expected_kind or source.event.ref != outcome.event.ref:
            _fail(
                "analytics_incompatible_source_event_ref",
                outcome,
                f"{name} does not name this card's {expected_kind.value}",
            )
        if outcome.event.data["version"] >= 2:
            if source.event.data.get("specification_revision") != outcome.event.data["specification_revision"]:
                _fail(
                    "analytics_incompatible_lineage_specification",
                    outcome,
                    f"{name} does not bind this outcome specification revision",
                )
    effect_id = refs["effect"]
    if effect_id is None:
        return
    effect = events.get(effect_id)
    if effect is None:
        _fail("analytics_dangling_source_event_ref", outcome, f"effect refers to absent event {effect_id!r}")
    if (
        effect.event.entity_kind is not EntityKind.CARD
        or effect.event.ref != outcome.event.ref
        or effect.event.source_state is None
    ):
        _fail(
            "analytics_incompatible_source_event_ref", outcome, "effect does not name this card's transition"
        )
    if outcome.event.data["version"] >= 2:
        owed = effect.event.data.get("attempt_outcome_owed")
        key = ("attempt_id", "attempt", "report_generation")
        if not isinstance(owed, dict) or any(owed.get(name) != outcome.event.data[name] for name in key):
            _fail(
                "analytics_incompatible_lineage_effect",
                outcome,
                "effect does not retain this outcome round identity",
            )


def _usage_source(
    outcome: _RecordedEvent,
    role: str,
    event_id: str | None,
    events: dict[str, _RecordedEvent],
) -> dict[str, Any] | None:
    if event_id is None:
        return None
    usage = events.get(event_id)
    if usage is None:
        _fail(
            "analytics_dangling_source_event_ref",
            outcome,
            f"{role} usage refers to absent event {event_id!r}",
        )
    event = usage.event
    data = event.data
    expected_phase = "worker" if role == "worker" else "review"
    same_round = (
        event.ref == outcome.event.ref
        and data.get("attempt_id") == outcome.event.data["attempt_id"]
        and data.get("attempt") == outcome.event.data["attempt"]
        and data.get("report_generation") == outcome.event.data["report_generation"]
    )
    if (
        event.kind is not EventKind.ATTEMPT_USAGE
        or event.entity_kind is not EntityKind.CARD
        or data.get("role") != role
        or data.get("phase") != expected_phase
        or not same_round
    ):
        _fail(
            "analytics_incompatible_usage_join",
            outcome,
            f"{role} usage must name this card, round, role and {expected_phase} phase",
        )
    return {"event_id": event.event_id, **data}


def _validate_usage_completeness(
    outcome: _RecordedEvent,
    completeness: dict[str, Any],
    worker: dict[str, Any] | None,
    review: dict[str, Any] | None,
) -> None:
    for role, usage in (("worker", worker), ("review", review)):
        state = completeness[role]
        if state in {"missing", "legacy"}:
            continue
        if usage is None:  # Defensive: typed outcome validation should make this unreachable.
            _fail("analytics_incompatible_usage_join", outcome, f"{role} {state} usage has no event")
        collected = usage["outcome"] == "collected"
        if (state == "collected") != collected:
            _fail(
                "analytics_incompatible_usage_join",
                outcome,
                f"{role} completeness {state!r} disagrees with usage outcome {usage['outcome']!r}",
            )


def _lineage_completeness(data: dict[str, Any]) -> dict[str, Any]:
    """Expose v2 forward-lineage gaps independently from provider usage."""
    if data["version"] == 1:
        return {"complete": True, "missing": []}
    required = data["lineage_required"]
    refs = data["source_event_ids"]
    missing = [
        name
        for name, required_now in required.items()
        if required_now
        and (data["specification_revision"] if name == "specification_revision" else refs[name]) is None
    ]
    return {"complete": not missing, "missing": missing}


def _incomplete_reasons(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["no_attempt_outcome_v1"]
    reasons: set[str] = set()
    for row in rows:
        if not row["lineage_completeness"]["complete"]:
            for name in row["lineage_completeness"]["missing"]:
                reasons.add(f"lineage_missing_{name}")
        if row["verdict"] == "legacy":
            reasons.add("legacy_outcome")
        for role, state in row["usage_completeness"].items():
            if state != "collected":
                reasons.add(f"{role}_usage_{state}")
    return sorted(reasons)


def _fail(code: str, recorded: _RecordedEvent, detail: str) -> None:
    raise AnalyticsProjectionError(code, recorded.path, recorded.number, detail)
