from __future__ import annotations

import json
from pathlib import Path

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
