from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

from secretary.web import provider_usage
from secretary.web.provider_usage import CLAUDE_USAGE_URL, CODEX_USAGE_URL, ProviderUsageLayer

NOW = 1_800_000_000.0


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_live_usage_is_compact_and_credentials_never_leave_headers(tmp_path: Path) -> None:
    write(tmp_path / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": "claude-secret"}})
    write(tmp_path / ".codex/auth.json", {"tokens": {"access_token": "codex-secret", "account_id": "acct"}})
    calls = []

    def fetch(url, headers, timeout):
        calls.append((url, headers, timeout))
        if url == CLAUDE_USAGE_URL:
            return {
                "five_hour": {"utilization": 0.26, "resets_at": NOW + 100},
                "seven_day": {"utilization": 72, "resets_at": NOW + 200},
            }
        assert url == CODEX_USAGE_URL
        return {
            "rate_limit": {
                "primary_window": {"used_percent": 41, "window_minutes": 300, "reset_at": NOW + 300},
                "secondary_window": {"used_percent": 9, "window_minutes": 10080, "reset_at": NOW + 400},
            }
        }

    layer = ProviderUsageLayer(home=tmp_path, fetch_json=fetch, now=lambda: NOW)
    document = layer.usage_snapshot()
    assert document["providers"][0]["windows"][0]["remaining_percent"] == 74.0
    assert document["providers"][1]["windows"][1]["remaining_percent"] == 91.0
    assert "secret" not in json.dumps(document)
    assert all(timeout == 3.0 for _, _, timeout in calls)
    assert layer.usage_snapshot() is document
    assert len(calls) == 2


def test_old_codex_session_is_truthfully_stale_when_live_read_fails(tmp_path: Path) -> None:
    write(tmp_path / ".codex/auth.json", {"tokens": {"access_token": "secret"}})
    session = tmp_path / ".codex/sessions/2026/01/01/rollout-test.jsonl"
    write(
        session,
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {
                "info": {
                    "rate_limits": {
                        "primary": {"used_percent": 80, "window_minutes": 10080, "resets_at": NOW + 10}
                    }
                }
            },
        },
    )

    def fail(*_args):
        raise TimeoutError

    document = ProviderUsageLayer(home=tmp_path, fetch_json=fail, now=lambda: NOW).usage_snapshot()
    codex = document["providers"][1]
    assert codex["status"] == "stale"
    assert codex["windows"][0]["remaining_percent"] == 20.0
    assert codex["reason"] == "Latest Codex usage observation is stale"


def test_missing_or_malformed_auth_is_explicitly_unavailable(tmp_path: Path) -> None:
    write(tmp_path / ".claude/.credentials.json", {"claudeAiOauth": {"accessToken": 3}})
    document = ProviderUsageLayer(home=tmp_path, now=lambda: NOW).usage_snapshot()
    assert [(item["id"], item["status"], item["windows"]) for item in document["providers"]] == [
        ("claude", "unavailable", []),
        ("codex", "unavailable", []),
    ]


# The Codex fallback's cost is bounded by constants, not by the size of ~/.codex/sessions.

NEWEST_DAY = date(2026, 9, 15)


def rate_limit_event(used: float, when: datetime) -> dict[str, object]:
    return {
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "payload": {"info": {"rate_limits": {"primary": {"used_percent": used, "window_minutes": 300}}}},
    }


def build_tree(
    home: Path,
    *,
    days: int,
    files_per_day: int,
    padding: int,
    event: Callable[[int, int], dict[str, object] | None],
) -> None:
    """Write sessions/YYYY/MM/DD/rollout-*.jsonl for `days` days ending on NEWEST_DAY.

    Each file is `padding` sparse bytes followed by a few complete lines; `event(day, index)` puts a
    rate-limit line last when it returns one.  Newer days and later file names get later mtimes.
    """
    for day_index in range(days):
        day = NEWEST_DAY - timedelta(days=day_index)
        directory = home / ".codex/sessions" / f"{day:%Y/%m/%d}"
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(files_per_day):
            started = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(minutes=30 * (1 + index))
            path = directory / f"rollout-{started:%Y-%m-%dT%H-%M-%S}-{index:04d}.jsonl"
            with path.open("wb") as handle:
                handle.truncate(padding)
                handle.seek(padding)
                handle.write(b'\n{"payload": {"type": "turn"}}\n')
                line = event(day_index, index)
                if line is not None:
                    handle.write(json.dumps(line).encode() + b"\n")
                handle.write(b'{"payload": {"type": "task_complete"}}\n')
            os.utime(path, (started.timestamp(), started.timestamp()))


def legacy_latest_codex_event(home: Path) -> tuple[dict[str, object], float] | None:
    """Today's unbounded walk before secretary-1662, kept here as the reference answer."""
    root = home / ".codex" / "sessions"
    files = sorted(root.rglob("rollout-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[:20]:
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                event = json.loads(line)
                limits = event["payload"]["info"]["rate_limits"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if isinstance(limits, dict):
                return limits, datetime.fromisoformat(event["timestamp"]).timestamp()
    return None


class CountingFile:
    def __init__(self, handle, counts: dict[str, int]) -> None:
        self.handle = handle
        self.counts = counts

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.handle.close()

    def seek(self, *args):
        return self.handle.seek(*args)

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self.counts["bytes"] += len(data)
        return data


class BoundedCodexFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        self.homes = tempfile.TemporaryDirectory()
        self.addCleanup(self.homes.cleanup)

    def home(self, name: str) -> Path:
        return Path(self.homes.name) / name

    def measure(self, home: Path) -> tuple[tuple[dict[str, object], float] | None, dict[str, int]]:
        counts = {"listings": 0, "opens": 0, "bytes": 0}
        real_scandir = os.scandir

        def scandir(path):
            counts["listings"] += 1
            return real_scandir(path)

        def counting_open(path, mode="r", *args, **kwargs):
            counts["opens"] += 1
            return CountingFile(open(path, mode, *args, **kwargs), counts)

        layer = ProviderUsageLayer(home=home, now=lambda: NOW)
        with (
            mock.patch("os.scandir", scandir),
            mock.patch.object(provider_usage, "open", counting_open, create=True),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")),
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file read")),
        ):
            found = layer._latest_codex_event()
        return found, counts

    def assert_within_constants(self, counts: dict[str, int]) -> None:
        self.assertLessEqual(counts["listings"], provider_usage.CODEX_FALLBACK_LISTINGS)
        self.assertLessEqual(counts["opens"], provider_usage.CODEX_FALLBACK_FILES)
        self.assertLessEqual(
            counts["bytes"], provider_usage.CODEX_FALLBACK_FILES * provider_usage.CODEX_TAIL_BYTES
        )

    def test_cost_is_fixed_when_the_tree_grows_tenfold_with_much_larger_files(self) -> None:
        tail = provider_usage.CODEX_TAIL_BYTES
        nothing = lambda _day, _index: None
        small, large = self.home("small"), self.home("large")
        build_tree(small, days=10, files_per_day=4, padding=2 * tail, event=nothing)
        build_tree(large, days=100, files_per_day=4, padding=400 * tail, event=nothing)
        self.assertEqual(
            len(list(large.rglob("rollout-*.jsonl"))), 10 * len(list(small.rglob("rollout-*.jsonl")))
        )

        small_found, small_counts = self.measure(small)
        large_found, large_counts = self.measure(large)

        self.assertIsNone(small_found)
        self.assertIsNone(large_found)
        # The worst case — no event anywhere — opens the full file budget and reads a full tail of each.
        self.assertEqual(small_counts["opens"], provider_usage.CODEX_FALLBACK_FILES)
        self.assertEqual(small_counts["bytes"], provider_usage.CODEX_FALLBACK_FILES * tail)
        self.assert_within_constants(small_counts)
        self.assert_within_constants(large_counts)
        self.assertEqual(large_counts, small_counts)

    def test_listing_count_does_not_change_with_ten_times_more_days(self) -> None:
        nothing = lambda _day, _index: None
        small, large = self.home("small"), self.home("large")
        build_tree(small, days=30, files_per_day=1, padding=0, event=nothing)
        build_tree(large, days=300, files_per_day=1, padding=0, event=nothing)
        _found, small_counts = self.measure(small)
        _found, large_counts = self.measure(large)
        self.assertEqual(large_counts, small_counts)
        self.assert_within_constants(large_counts)

    def test_the_newest_event_within_bounds_is_the_legacy_answer(self) -> None:
        home = self.home("tree")

        def event(day: int, index: int) -> dict[str, object] | None:
            if day == 0 or (day == 1 and index == 3):
                return None  # the newest files carry no rate-limit line
            midnight = datetime.combine(NEWEST_DAY - timedelta(days=day), datetime.min.time(), UTC)
            return rate_limit_event(10 * day + index, midnight + timedelta(minutes=30 * (1 + index) + 5))

        build_tree(home, days=20, files_per_day=4, padding=3 * provider_usage.CODEX_TAIL_BYTES, event=event)
        found, counts = self.measure(home)
        self.assertEqual(found, legacy_latest_codex_event(home))
        assert found is not None
        self.assertEqual(found[0]["primary"]["used_percent"], 12)
        self.assertEqual(counts["opens"], 6)
        self.assert_within_constants(counts)

    def test_a_long_line_ending_the_tail_is_parsed_and_a_cut_line_is_skipped(self) -> None:
        home = self.home("tree")
        build_tree(home, days=1, files_per_day=1, padding=0, event=lambda _d, _i: None)
        path = next(home.rglob("rollout-*.jsonl"))
        cut = rate_limit_event(99, datetime(2026, 9, 15, tzinfo=UTC))
        cut["pad"] = "x" * provider_usage.CODEX_TAIL_BYTES
        whole = rate_limit_event(55, datetime(2026, 9, 15, 2, tzinfo=UTC))
        whole["pad"] = "y" * (provider_usage.CODEX_TAIL_BYTES // 2)
        path.write_text(json.dumps(cut) + "\n" + json.dumps(whole) + "\n", encoding="utf-8")
        found, _counts = self.measure(home)
        assert found is not None
        self.assertEqual(found[0]["primary"]["used_percent"], 55)
        path.write_text(json.dumps(cut) + "\n", encoding="utf-8")
        self.assertIsNone(self.measure(home)[0])

    def test_an_event_beyond_the_tail_is_reported_unavailable_not_read_for(self) -> None:
        home = self.home("tree")
        build_tree(home, days=1, files_per_day=1, padding=0, event=lambda _d, _i: None)
        path = next(home.rglob("rollout-*.jsonl"))
        buried = json.dumps(rate_limit_event(50, datetime(2026, 9, 15, tzinfo=UTC)))
        filler = "\n".join(['{"payload": {"type": "turn"}}'] * (provider_usage.CODEX_TAIL_BYTES // 16))
        path.write_text(buried + "\n" + filler + "\n", encoding="utf-8")
        self.assertIsNotNone(legacy_latest_codex_event(home))  # today's code reads the whole file for it
        found, counts = self.measure(home)
        self.assertIsNone(found)
        self.assertEqual(counts["bytes"], provider_usage.CODEX_TAIL_BYTES)

        def fail(*_args):
            raise TimeoutError

        write(home / ".codex/auth.json", {"tokens": {"access_token": "secret"}})
        document = ProviderUsageLayer(home=home, fetch_json=fail, now=lambda: NOW).usage_snapshot()
        codex = document["providers"][1]
        self.assertEqual(codex["status"], "unavailable")
        self.assertEqual(codex["reason"], "Codex usage is temporarily unavailable")

    def test_empty_day_directories_exhaust_the_listing_budget_and_stop(self) -> None:
        home = self.home("tree")
        build_tree(
            home,
            days=1,
            files_per_day=1,
            padding=0,
            event=lambda _d, _i: rate_limit_event(50, datetime(2025, 1, 1, tzinfo=UTC)),
        )
        # Move that one rollout into an old day behind a long run of newer, empty days.
        rollout = next(home.rglob("rollout-*.jsonl"))
        old_day = home / ".codex/sessions/2025/01/01"
        old_day.mkdir(parents=True)
        rollout.rename(old_day / rollout.name)
        for offset in range(provider_usage.CODEX_FALLBACK_LISTINGS * 2):
            day = NEWEST_DAY - timedelta(days=offset)
            (home / ".codex/sessions" / f"{day:%Y/%m/%d}").mkdir(parents=True, exist_ok=True)
        found, counts = self.measure(home)
        self.assertIsNone(found)
        self.assertEqual(counts["listings"], provider_usage.CODEX_FALLBACK_LISTINGS)
        self.assertEqual(counts["opens"], 0)
