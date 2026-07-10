from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


LAST_FETCH_NAME = "last_fetch"
MAX_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class OffsiteStatus:
    warnings: list[str]
    findings: list[str]


def check_last_fetch(
    instance: dict[str, Any],
    *,
    now: datetime | None = None,
) -> OffsiteStatus:
    """Check the offsite pull marker when a max age is configured."""
    offsite = instance.get("offsite")
    if not isinstance(offsite, dict):
        return OffsiteStatus([], [])

    max_age_days = offsite.get("backup_pull_max_age_days")
    if not isinstance(max_age_days, int):
        return OffsiteStatus([], [])

    data_dir = instance.get("data_dir")
    if not isinstance(data_dir, str) or not data_dir:
        return OffsiteStatus([], [])

    marker = last_fetch_path(Path(data_dir).expanduser())
    try:
        marker_exists = marker.is_file()
    except OSError:
        return OffsiteStatus([], ["offsite backup pull marker is unavailable"])
    if not marker_exists:
        return OffsiteStatus(["offsite backup pull is not configured: last_fetch missing"], [])

    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return OffsiteStatus([], ["offsite backup pull marker is unavailable"])
    except UnicodeError:
        return OffsiteStatus([], ["offsite backup pull marker is invalid"])

    fetched_at = _parse_fetch_time(raw)
    if fetched_at is None:
        return OffsiteStatus([], ["offsite backup pull marker is invalid"])

    now = _as_utc(now or datetime.now(UTC))
    age = now - fetched_at
    if age < -MAX_CLOCK_SKEW:
        return OffsiteStatus([], ["offsite backup pull marker is in the future"])
    if age > timedelta(days=max_age_days):
        return OffsiteStatus(
            [],
            [
                "offsite backup pull is stale: "
                f"last_fetch age {age.days}d exceeds {max_age_days}d"
            ],
        )
    return OffsiteStatus([], [])


def last_fetch_path(data_dir: Path) -> Path:
    return data_dir / "backups" / LAST_FETCH_NAME


def _parse_fetch_time(raw: str) -> datetime | None:
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        return _as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
