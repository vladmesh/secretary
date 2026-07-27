"""Read the production dispatcher's durable tick telemetry (secretary-833).

The dispatcher that actually moves cards on this host is
`secretary dispatcher production-tick`, run by secretary-dispatcher-production.timer. It records
how each terminal tick ended under `tick_telemetry` in its own state file — see
`secretary.dispatcher_production.record_tick_telemetry` for the writer and the field meanings.

Both readers of that record live outside the dispatcher's own process: `runtime/health.py` builds
the pipeline health line from it, and the steward's `signals.py` reports its unhealthy ticks. They
share this module so there is one place that knows where the file is and what a missing or
unusable one means.

Read through the file, never by importing the dispatcher: an agent's unit runs in its own worktree
with its own environment, and the state file is the durable boundary between the two — the same
reasoning that already sends the steward across worktrees for the dispatcher's own state
(triggered-agents-253).

Path resolution mirrors `runtime/state.py`'s STATE_ROOT: the installation's data dir, which every
packaged unit leaves at its default under the runtime user's home. `TA_PRODUCTION_STATE` (whole
file) and `SECRETARY_DATA_DIR` (the data dir) override it for tests and a host whose layout
diverges.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def data_dir() -> Path:
    configured = os.environ.get("SECRETARY_DATA_DIR")
    return Path(configured) if configured else Path.home() / "secretary-data"


def state_path() -> Path:
    """Recomputed per call, not frozen at import, so a test (or a unit with its own env) sees the
    override it just set — same reasoning as signals.resolve_pipeline_state_dir()."""
    configured = os.environ.get("TA_PRODUCTION_STATE")
    return Path(configured) if configured else data_dir() / "dispatcher" / "production-state.json"


@dataclass(frozen=True)
class TickTelemetry:
    """What the dispatcher durably recorded about its ticks, or why nothing could be read.

    `unavailable` carries the reason as a short slug, and is never silently the same as "the
    dispatcher is fine and quiet": a caller that cannot tell a healthy pipeline from an unreadable
    one is exactly the blindness this record exists to end.
    """

    path: Path
    unavailable: str = ""
    last: dict = field(default_factory=dict)
    last_healthy_at: str = ""
    unhealthy: tuple[dict, ...] = ()
    unhealthy_total: int = 0
    tick_seq: int = 0

    @property
    def available(self) -> bool:
        return not self.unavailable


def read() -> TickTelemetry:
    path = state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return TickTelemetry(path, unavailable="production-state-missing")
    except (OSError, ValueError, UnicodeError):
        return TickTelemetry(path, unavailable="production-state-unreadable")
    telemetry = payload.get("tick_telemetry") if isinstance(payload, dict) else None
    if not isinstance(telemetry, dict):
        # The state file exists but carries no tick telemetry: a dispatcher that has not ticked
        # since this record was introduced, or a host still running an older product.
        return TickTelemetry(path, unavailable="tick-telemetry-missing")
    last = telemetry.get("last")
    unhealthy = telemetry.get("unhealthy")
    return TickTelemetry(
        path=path,
        last=last if isinstance(last, dict) else {},
        last_healthy_at=str(telemetry.get("last_healthy_at") or ""),
        unhealthy=tuple(item for item in (unhealthy or []) if isinstance(item, dict))
        if isinstance(unhealthy, list) else (),
        unhealthy_total=_counter(telemetry.get("unhealthy_total")),
        tick_seq=_counter(telemetry.get("tick_seq")),
    )


def _counter(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def describe(entry: dict) -> str:
    """One-line diagnostic for a recorded tick: its status, and what actually went wrong."""
    parts = [str(entry.get("status") or "unknown")]
    reason = str(entry.get("reason") or "")
    if reason:
        parts.append(reason)
    errors = [item for item in (entry.get("errors") or []) if isinstance(item, dict)]
    for error in errors:
        ref = str(error.get("ref") or "")
        code = str(error.get("code") or "error")
        parts.append(f"{ref} {code}".strip())
    hidden = _counter(entry.get("error_count")) - len(errors)
    if hidden > 0:
        parts.append(f"+{hidden} more error(s)")
    return "; ".join(parts)
