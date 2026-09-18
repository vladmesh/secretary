"""One projection of headless work in progress, for every surface that reports it.

A card standing in an active column whose worker no dispatcher record can name is degraded, and
saying so is the whole point of `worker_headless` (secretary-1544). It has to be said in each place
an operator actually looks: the installation snapshot (`secretary status`) and the sprint summary
(`secretary sprint status`, the command the observer skill opens with).

It lives here rather than in `status.py` because the first round put it there and `sprint status`
then silently answered `degraded_cards: {}` for every sprint — an affirmative claim of health in the
one surface the actor who creates this state reads. Two call sites cannot disagree about a fact they
both take from one function, and this module deliberately imports nothing heavy, so a CLI path that
only wants the projection does not pull in the doctor stack to get it. It sits in
`secretary.dispatch` because that is where dispatcher-adjacent vocabulary belongs; the flat package
root is closed to new modules (docs/ARCHITECTURE.md, "Source layout and module boundaries").
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from secretary.dispatcher_state import HeadlessRecoveryEpisode


def headless_worker(record: dict[str, Any]) -> dict[str, Any] | None:
    """Work a board column shows as in progress that no head on this record is doing.

    None when the card owns a worker identity. Otherwise the whole degradation an operator needs
    without opening a transcript: the record state, that there is no handle and no heartbeat, how
    long it has been like that, and the retained checkout and candidate a recovery would bind.
    """
    episode = HeadlessRecoveryEpisode.from_json(record.get("worker_headless"))
    if episode is None:
        return None
    return {
        "state": episode.record_state or None,
        "handle_known": episode.handle_known,
        "heartbeat": episode.heartbeat or None,
        "since": _epoch(episode.since),
        "waiting_seconds": max(0, int(time.time() - episode.since)) if episode.since else None,
        "workspace": episode.workspace or None,
        "branch": episode.branch or None,
        "expected_branch": episode.expected_branch or None,
        "dirty": episode.dirty,
        "candidate_sha": episode.candidate_sha or None,
        "report_generation": episode.report_generation,
        "recovery_error": episode.recovery_error or None,
    }


def headless_cards(production: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every card in this production state whose worker no record can name, keyed by reference.

    Takes the raw `production-state.json` object, which is what both callers already hold.
    """
    records = production.get("records")
    if not isinstance(records, dict):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for reference, record in records.items():
        if not isinstance(reference, str) or not isinstance(record, dict):
            continue
        detail = headless_worker(record)
        if detail is not None:
            found[reference] = detail
    return found


def _epoch(value: float) -> str | None:
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None
