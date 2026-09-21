"""The tick's budget pass reads a window of the audit, not its history (secretary-1658).

`_reconcile_sprint_budget` keeps the lower bound of its next window in the production state. A
pass with no bound reads the last `BUDGET_WINDOW_BOOTSTRAP` and sets one; a pass with a bound asks
the audit only for what settled since; a pass that leaves an event eligible keeps the old bound so
the event is read again. No pass reads the whole history. Whether an event was already charged is
its own request id's lookup, not a set built from the read.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest import mock

from secretary.dispatch import production
from secretary.dispatch.production import (
    BUDGET_WINDOW_BOOTSTRAP,
    BUDGET_WINDOW_KEY,
    BUDGET_WINDOW_OVERLAP,
    _reconcile_sprint_budget,
)
from secretary.tasks import TaskError


def _hotfix(identity: str) -> dict[str, Any]:
    return {
        "request_id": identity,
        "event_id": identity,
        "ref": "secretary-5",
        "kind": "created",
        "payload": {"sprint": "sprint:9", "budget_event": "hotfix"},
    }


def _preempt(identity: str) -> dict[str, Any]:
    return {
        "request_id": identity,
        "event_id": identity,
        "ref": "secretary-6",
        "kind": "moved",
        "payload": {"from": "in_progress", "to": "ready"},
    }


class _Audit:
    def __init__(self, events: list[dict[str, Any]], committed: set[str] = frozenset()) -> None:
        self.records = events
        self.committed = set(committed)
        self.reads: list[dict[str, Any]] = []
        self.board_dir = "/nonexistent/board"

    def events(self, reference: str = "", **filters: Any) -> list[dict[str, Any]]:
        self.reads.append({"reference": reference, **filters})
        if not reference and filters.get("since") is None and filters.get("references") is None:
            raise AssertionError("the budget pass read the whole audit")
        return list(self.records)

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return {"request_id": request_id} if request_id in self.committed else None


class _Writer:
    charged: ClassVar[list[str]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def record_budget(self, **kwargs: Any) -> dict[str, Any]:
        _Writer.charged.append(kwargs["request_id"])
        return {"sprint": {"status": "open"}}


class BudgetWindowTests(unittest.TestCase):
    def runtime(self, audit: _Audit) -> SimpleNamespace:
        def show(reference: str) -> dict[str, Any]:
            raise TaskError("backend_unavailable", f"{reference} is unavailable", 1)

        return SimpleNamespace(
            catalog=SimpleNamespace(instance={}),
            reader=SimpleNamespace(client=object(), show=show),
            data_dir=Path("/nonexistent"),
            owner="dispatcher-test",
            audit=audit,
        )

    def setUp(self) -> None:
        _Writer.charged = []
        patcher = mock.patch.object(production, "SprintWriter", _Writer)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_bootstrap_read(self, audit: _Audit, before: datetime) -> None:
        (read,) = audit.reads
        self.assertEqual(set(read), {"reference", "since"})
        self.assertGreaterEqual(read["since"], before - BUDGET_WINDOW_BOOTSTRAP)
        self.assertLessEqual(read["since"], datetime.now(UTC) - BUDGET_WINDOW_BOOTSTRAP)

    def test_a_pass_without_a_bound_reads_the_bootstrap_window_and_sets_one(self) -> None:
        audit = _Audit([_hotfix("evt-1")])
        payload: dict[str, Any] = {}
        before = datetime.now(UTC)

        _reconcile_sprint_budget(self.runtime(audit), payload)

        self.assert_bootstrap_read(audit, before)
        self.assertEqual(_Writer.charged, ["sprint-budget-evt-1"])
        since = datetime.fromisoformat(payload[BUDGET_WINDOW_KEY]["since"])
        self.assertGreaterEqual(since, before - BUDGET_WINDOW_OVERLAP)
        self.assertLessEqual(since, datetime.now(UTC) - BUDGET_WINDOW_OVERLAP)

    def test_a_pass_with_a_bound_reads_only_the_window_and_skips_what_is_charged(self) -> None:
        bound = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
        audit = _Audit([_hotfix("evt-1"), _hotfix("evt-2")], committed={"sprint-budget-evt-1"})
        payload: dict[str, Any] = {BUDGET_WINDOW_KEY: {"since": bound.isoformat()}}

        _reconcile_sprint_budget(self.runtime(audit), payload)

        self.assertEqual(audit.reads, [{"reference": "", "since": bound}])
        self.assertEqual(_Writer.charged, ["sprint-budget-evt-2"])
        self.assertGreater(datetime.fromisoformat(payload[BUDGET_WINDOW_KEY]["since"]), bound)

    def test_an_event_left_eligible_keeps_the_old_bound(self) -> None:
        bound = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
        audit = _Audit([_preempt("evt-3")])
        payload: dict[str, Any] = {BUDGET_WINDOW_KEY: {"since": bound.isoformat()}}

        _reconcile_sprint_budget(self.runtime(audit), payload)

        self.assertEqual(_Writer.charged, [])
        self.assertEqual(payload[BUDGET_WINDOW_KEY], {"since": bound.isoformat()})

    def test_an_event_left_eligible_on_a_first_pass_keeps_the_bootstrap_bound(self) -> None:
        audit = _Audit([_preempt("evt-3")])
        payload: dict[str, Any] = {}

        _reconcile_sprint_budget(self.runtime(audit), payload)

        self.assertEqual(payload[BUDGET_WINDOW_KEY], {"since": audit.reads[0]["since"].isoformat()})

    def test_an_unreadable_bound_reads_the_bootstrap_window(self) -> None:
        for window in ({"since": "yesterday"}, {"since": "2026-09-21T08:00:00"}, "2026", {}):
            with self.subTest(window=window):
                audit = _Audit([])
                before = datetime.now(UTC)
                _reconcile_sprint_budget(self.runtime(audit), {BUDGET_WINDOW_KEY: window})
                self.assert_bootstrap_read(audit, before)

    def test_without_the_production_state_the_window_is_the_bootstrap(self) -> None:
        audit = _Audit([_hotfix("evt-1")])
        before = datetime.now(UTC)

        _reconcile_sprint_budget(self.runtime(audit))

        self.assert_bootstrap_read(audit, before)


if __name__ == "__main__":
    unittest.main()
