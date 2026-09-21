"""The tick's budget pass reads the audit through one durable cursor, a page per tick (secretary-1658).

`_reconcile_sprint_budget` keeps its cursor in the production state: the audit position it has
passed and the request ids it passed without resolving (the deferred set). A pass without a cursor
starts at the beginning of history; every pass reads one bounded page past the cursor and retries
the deferred set by request id. Nothing is dropped for being old, and nothing is charged twice:
whether an event was charged is its charge's request-id lookup, not a set built from a read.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from secretary.dispatch import production
from secretary.dispatch.production import (
    BUDGET_CURSOR_KEY,
    BUDGET_CURSOR_SETTLE_MARGIN,
    BUDGET_PAGE_LIMIT,
    _reconcile_sprint_budget,
)
from secretary.tasks import TaskError

NOW = datetime.now(UTC)


def _preempt(identity: str, ref: str = "secretary-6") -> dict[str, Any]:
    return {
        "request_id": identity,
        "event_id": identity,
        "ref": ref,
        "kind": "moved",
        "payload": {"from": "in_progress", "to": "ready"},
    }


def _comment(identity: str) -> dict[str, Any]:
    return {"request_id": identity, "event_id": identity, "ref": "secretary-7", "kind": "commented"}


class _Audit:
    """The committed audit in claim order, answering only the cursor page and primary-key reads."""

    board_dir = "/nonexistent/board"

    def __init__(self) -> None:
        self.rows: list[tuple[datetime, dict[str, Any]]] = []
        self.charges: dict[str, dict[str, Any]] = {}
        self.pages: list[tuple[list[str] | None, int]] = []
        self.lookups: list[str] = []

    def add(self, settled: datetime, event: dict[str, Any]) -> None:
        self.rows.append((settled, event))
        self.rows.sort(key=lambda row: (row[0], row[1]["request_id"]))

    def events(self, *_args: Any, **_filters: Any) -> list[dict[str, Any]]:
        raise AssertionError("the budget pass read the audit other than through its cursor")

    def events_page(self, **_options: Any) -> Any:
        raise AssertionError("the budget pass read the audit other than through its cursor")

    @staticmethod
    def position(settled: datetime, event: dict[str, Any]) -> list[str]:
        return [settled.isoformat(), settled.isoformat(), event["request_id"]]

    def events_after(self, after: list[str] | None, *, limit: int) -> list[Any]:
        self.pages.append((after, limit))
        keys = [(self.position(settled, event), settled, event) for settled, event in self.rows]
        if after is not None:
            bound = (datetime.fromisoformat(after[0]), after[2])
            keys = [row for row in keys if (row[1], row[2]["request_id"]) > bound]
        return keys[:limit]

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        self.lookups.append(request_id)
        if request_id in self.charges:
            return self.charges[request_id]
        return next((event for _settled, event in self.rows if event["request_id"] == request_id), None)


class _Writer:
    audit: _Audit
    charged: list[str]

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def record_budget(self, **kwargs: Any) -> dict[str, Any]:
        request_id = kwargs["request_id"]
        assert request_id not in _Writer.audit.charges, f"{request_id} charged twice"
        _Writer.audit.charges[request_id] = {"request_id": request_id}
        _Writer.charged.append(request_id)
        return {"sprint": {"status": "open"}}


class BudgetCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit = _Audit()
        self.unavailable: set[str] = set()
        _Writer.audit, _Writer.charged = self.audit, []
        patcher = mock.patch.object(production, "SprintWriter", _Writer)
        patcher.start()
        self.addCleanup(patcher.stop)

        def show(reference: str) -> dict[str, Any]:
            if reference in self.unavailable:
                raise TaskError("backend_unavailable", f"{reference} is unavailable", 1)
            return {"ref": reference, "sprint": "sprint:9"}

        self.runtime = SimpleNamespace(
            catalog=SimpleNamespace(instance={}),
            reader=SimpleNamespace(client=object(), show=show),
            data_dir=Path("/nonexistent"),
            owner="dispatcher-test",
            audit=self.audit,
        )

    def tick(self, payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        self.audit.pages.clear()
        outcomes = _reconcile_sprint_budget(self.runtime, payload)
        self.assertEqual(len(self.audit.pages), 1, "one page per tick")
        self.assertEqual(self.audit.pages[0][1], BUDGET_PAGE_LIMIT)
        return outcomes

    def test_the_reviewers_case_an_old_event_deferred_before_deployment_is_charged_once(self) -> None:
        # Settled eight days ago; its card lookup failed under the old code, so nothing charged it.
        # The card has recovered, and the production state the old code left has no cursor.
        self.audit.add(NOW - timedelta(days=8), _preempt("evt-old"))
        self.audit.add(NOW - timedelta(days=2), _comment("evt-later"))
        payload: dict[str, Any] = {"sprint_budget_window": {"since": (NOW - timedelta(days=1)).isoformat()}}

        self.tick(payload)

        self.assertIsNone(self.audit.pages[0][0], "a state without a cursor starts at the beginning")
        self.assertEqual(_Writer.charged, ["sprint-budget-evt-old"])
        for _ in range(3):
            self.tick(payload)
        self.assertEqual(_Writer.charged, ["sprint-budget-evt-old"])

    def test_an_event_deferred_longer_than_any_window_is_charged_once_when_its_card_recovers(self) -> None:
        self.audit.add(NOW - timedelta(days=90), _preempt("evt-stuck", ref="secretary-stuck"))
        self.audit.add(NOW - timedelta(days=89), _preempt("evt-fine", ref="secretary-fine"))
        self.unavailable.add("secretary-stuck")
        payload: dict[str, Any] = {}

        self.tick(payload)

        # The cursor is past both; the one it could not resolve is in the deferred set.
        self.assertEqual(payload[BUDGET_CURSOR_KEY]["deferred"], ["evt-stuck"])
        self.assertEqual(payload[BUDGET_CURSOR_KEY]["after"][2], "evt-fine")
        self.assertEqual(_Writer.charged, ["sprint-budget-evt-fine"])
        # Months of history arrive while the card stays unreadable; every tick retries it.
        for day in range(60):
            self.audit.add(NOW - timedelta(days=88 - day), _comment(f"evt-noise-{day}"))
            self.tick(payload)
            self.assertEqual(payload[BUDGET_CURSOR_KEY]["deferred"], ["evt-stuck"])
        self.assertIn("evt-stuck", self.audit.lookups, "retried by its request id")

        self.unavailable.clear()
        self.tick(payload)
        self.tick(payload)

        self.assertEqual(_Writer.charged, ["sprint-budget-evt-fine", "sprint-budget-evt-stuck"])
        self.assertEqual(payload[BUDGET_CURSOR_KEY]["deferred"], [])

    def test_a_bootstrap_over_a_large_history_reads_one_page_per_tick_and_charges_nothing_twice(self) -> None:
        total = 3 * BUDGET_PAGE_LIMIT + 17
        start = NOW - timedelta(days=70)
        for index in range(total):
            event = _preempt(f"evt-{index:05d}") if index % 3 == 0 else _comment(f"evt-{index:05d}")
            self.audit.add(start + timedelta(minutes=index), event)
        # A third of the budget events were charged by the old whole-history passes.
        for index in range(0, total, 9):
            self.audit.charges[f"sprint-budget-evt-{index:05d}"] = {}
        owed = {f"sprint-budget-evt-{index:05d}" for index in range(0, total, 3) if index % 9}
        payload: dict[str, Any] = {}

        afters = []
        for _ in range(6):
            self.tick(payload)
            afters.append(self.audit.pages[0][0])

        self.assertEqual(afters[0], None)
        self.assertEqual([after[2] for after in afters[1:4]], [f"evt-{n * BUDGET_PAGE_LIMIT - 1:05d}" for n in (1, 2, 3)])
        self.assertEqual(afters[4], afters[5], "caught up: the read past the cursor is empty")
        self.assertEqual(len(_Writer.charged), len(set(_Writer.charged)))
        self.assertEqual(set(_Writer.charged), owed)

    def test_normal_increments_charge_new_events_and_keep_the_unsettled_tail_in_front(self) -> None:
        self.audit.add(NOW - timedelta(days=1), _preempt("evt-a"))
        payload: dict[str, Any] = {}
        self.tick(payload)
        passed = payload[BUDGET_CURSOR_KEY]["after"]
        self.assertEqual(passed[2], "evt-a")

        # Just settled: charged now, but the cursor stays in front of it until it cannot be
        # overtaken by a record that settled before it and commits later.
        self.audit.add(datetime.now(UTC), _preempt("evt-b", ref="secretary-8"))
        self.tick(payload)
        self.assertEqual(self.audit.pages[0][0], passed)
        self.assertEqual(payload[BUDGET_CURSOR_KEY]["after"], passed)
        self.audit.add(datetime.now(UTC) - BUDGET_CURSOR_SETTLE_MARGIN / 2, _preempt("evt-late", ref="secretary-9"))
        self.tick(payload)

        self.assertEqual(_Writer.charged, ["sprint-budget-evt-a", "sprint-budget-evt-b", "sprint-budget-evt-late"])
        self.assertEqual(payload[BUDGET_CURSOR_KEY]["after"], passed)

    def test_an_event_deferred_then_recovered_on_the_next_tick_is_charged_once(self) -> None:
        self.audit.add(NOW - timedelta(days=1), _preempt("evt-a"))
        self.unavailable.add("secretary-6")
        payload: dict[str, Any] = {}
        self.tick(payload)
        self.assertEqual(payload[BUDGET_CURSOR_KEY]["deferred"], ["evt-a"])

        self.unavailable.clear()
        self.tick(payload)
        self.tick(payload)

        self.assertEqual(_Writer.charged, ["sprint-budget-evt-a"])
        self.assertEqual(payload[BUDGET_CURSOR_KEY]["deferred"], [])

    def test_an_unreadable_cursor_starts_at_the_beginning(self) -> None:
        self.audit.add(NOW - timedelta(days=30), _preempt("evt-a"))
        for cursor in ({"after": "2026"}, {"after": []}, "2026", {}, {"deferred": "evt-a"}):
            with self.subTest(cursor=cursor):
                self.tick({BUDGET_CURSOR_KEY: cursor})
                self.assertIsNone(self.audit.pages[0][0])
        self.assertEqual(_Writer.charged, ["sprint-budget-evt-a"])

    def test_without_the_production_state_the_pass_reads_the_first_page(self) -> None:
        self.audit.add(NOW - timedelta(days=30), _preempt("evt-a"))

        self.tick(None)

        self.assertIsNone(self.audit.pages[0][0])
        self.assertEqual(_Writer.charged, ["sprint-budget-evt-a"])


if __name__ == "__main__":
    unittest.main()
