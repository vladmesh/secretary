"""Read the production dispatcher's durable tick telemetry.

The dispatcher that actually moves cards on this host is `secretary dispatcher production-tick`.
It records how each terminal tick ended under `tick_telemetry` in its own state file — see
`secretary.dispatcher_production.record_tick_telemetry` for the writer and the field meanings.

Both readers of that record live outside the dispatcher's process: `runtime/health.py` and the
steward's `signals.py`. They share this module so there is one place that knows where the file is
and what a missing or unusable one means. The dispatcher's resource-probe cache is resolved here
too (`resource_health_path`).

Read through the file, never by importing the dispatcher: an agent's unit runs in its own
worktree with its own environment, and the state file is the durable boundary between the two.

Path resolution follows the dispatcher's own, so the reader lands on the file the writer writes on
any installation: `--data-dir`/`SECRETARY_DATA_DIR` first, then `data_dir` out of the instance the
dispatcher unit is started with. A reader's own unit gets that instance from `SECRETARY_INSTANCE`,
falling back to the directory of its rendered `TA_RUNTIME_ENV_FILE`. `TA_PRODUCTION_STATE`
overrides the whole file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from secretary.config import DataDirError
from secretary.config import instance_data_dir as _configured_data_dir
from triggered_agents.runtime.paths import default_instance_path

DEFAULT_INSTANCE = default_instance_path()


def _instance_from_runtime_env() -> Path | None:
    """The instance dir implied by the role unit's own env-file path, or None if it has none.

    Every packaged agent unit is rendered with `TA_RUNTIME_ENV_FILE=<instance>/runtime.env`, so on an
    installation whose instance is not the default one, a role that was never handed
    SECRETARY_INSTANCE still resolves the instance the dispatcher runs against.
    """
    configured = os.environ.get("TA_RUNTIME_ENV_FILE")
    return Path(configured).expanduser().parent if configured else None


def instance_file() -> Path:
    configured = os.environ.get("SECRETARY_INSTANCE")
    path = Path(configured).expanduser() if configured else (_instance_from_runtime_env() or DEFAULT_INSTANCE)
    return path / "instance.yaml" if path.is_dir() else path


def instance_data_dir() -> Path | None:
    """The installation's `data_dir` as the dispatcher resolves it, or None if it cannot be read.

    None is not an error: the caller falls back to the home default and reports the path it looked
    at. An instance that cannot be parsed must not take down the health command.
    """
    try:
        return _configured_data_dir(instance_file())
    except DataDirError:
        return None


def data_dir() -> Path:
    configured = os.environ.get("SECRETARY_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return instance_data_dir() or Path.home() / "secretary-data"


def state_path() -> Path:
    """Recomputed per call, not frozen at import, so a test (or a unit with its own env) sees the
    override it just set — an import-time constant would freeze the wrong data plane for any
    process that resolves its environment after import."""
    configured = os.environ.get("TA_PRODUCTION_STATE")
    return Path(configured) if configured else data_dir() / "dispatcher" / "production-state.json"


def resource_health_path() -> Path:
    """The resource-probe cache the production dispatcher keeps, resolved like `state_path()`.

    Writer: `secretary.head_health.HeadHealth`, next to the dispatcher's own state under
    `<data_dir>/dispatcher/`. The steward reads it to see the verdict the dispatcher actually acted
    on instead of running its own paid probes. `TA_PRODUCTION_RESOURCE_HEALTH` overrides the file.
    """
    configured = os.environ.get("TA_PRODUCTION_RESOURCE_HEALTH")
    return Path(configured) if configured else data_dir() / "dispatcher" / "resource_health.json"


def read_resource_status() -> dict[str, str] | None:
    """{resource: status} from the dispatcher's cache, or None when it cannot be read.

    None is "nothing was measured", distinct from an empty cache: the caller keeps its previous
    baseline rather than resetting it, so a transient unreadable file cannot erase a flip.
    """
    try:
        cache = json.loads(resource_health_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(cache, dict):
        return None
    try:
        return {str(rid): str(entry["status"]) for rid, entry in cache.items()}
    except (KeyError, TypeError):
        return None


@dataclass(frozen=True)
class TickTelemetry:
    """What the dispatcher durably recorded about its ticks, or why nothing could be read.

    `unavailable` carries the reason as a short slug and is never silently the same as "the
    dispatcher is fine and quiet".
    """

    path: Path
    unavailable: str = ""
    last: dict = field(default_factory=dict)
    last_healthy_at: str = ""
    unhealthy: tuple[dict, ...] = ()
    unhealthy_total: int = 0
    tick_seq: int = 0
    # The open incident (one continuous run of unhealthy ticks) or {} when the pipeline is not in
    # one, and the last closed one with the healthy tick that ended it. The counters next to them
    # are monotonic within a generation, so a reader can dedupe on them the way it dedupes on
    # `unhealthy_total` — see `secretary.dispatcher_production._record_incident`.
    incident: dict = field(default_factory=dict)
    incident_total: int = 0
    recovery: dict = field(default_factory=dict)
    recovery_total: int = 0
    # Which telemetry history the counters above belong to. Empty means the writer never stamped
    # one (a host still on an older product); a reader that dedupes on the counters must treat
    # that as "cannot tell" rather than as a change.
    generation: str = ""

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
    incident = telemetry.get("incident")
    recovery = telemetry.get("recovery")
    return TickTelemetry(
        path=path,
        last=last if isinstance(last, dict) else {},
        last_healthy_at=str(telemetry.get("last_healthy_at") or ""),
        unhealthy=tuple(item for item in (unhealthy or []) if isinstance(item, dict))
        if isinstance(unhealthy, list)
        else (),
        unhealthy_total=_counter(telemetry.get("unhealthy_total")),
        tick_seq=_counter(telemetry.get("tick_seq")),
        generation=str(telemetry.get("generation") or ""),
        incident=incident if isinstance(incident, dict) else {},
        incident_total=_counter(telemetry.get("incident_total")),
        recovery=recovery if isinstance(recovery, dict) else {},
        recovery_total=_counter(telemetry.get("recovery_total")),
    )


def _counter(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def describe(entry: dict) -> str:
    """One-line diagnostic for a recorded tick: its status, and what actually went wrong.

    Both kinds of failure are named: the errors the tick caught, and the action outcomes that
    reported a failed operation without raising. A tick red only because of the latter would
    otherwise print its status and nothing an operator can act on.
    """
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
    degradations = [item for item in (entry.get("degradations") or []) if isinstance(item, dict)]
    for degradation in degradations:
        ref = str(degradation.get("ref") or "")
        what = str(degradation.get("action") or degradation.get("step") or "degraded action")
        detail = str(degradation.get("reason") or "")
        parts.append(" ".join(part for part in (ref, what, detail) if part))
    hidden = _counter(entry.get("degraded_count")) - len(degradations)
    if hidden > 0:
        parts.append(f"+{hidden} more degraded action(s)")
    return "; ".join(parts)


def describe_incident(incident: dict) -> str:
    """One-line diagnostic for an incident: when it started, how long it ran, what caused it.

    The cause is the tick that opened it, not the newest failing one: every tick of a Kanboard outage
    says the same thing, and what an operator needs is where it began.
    """
    if not incident:
        return ""
    opened = incident.get("opened")
    parts = [
        f"since {incident.get('opened_at') or '?'}",
        f"{_counter(incident.get('unhealthy_ticks'))} unhealthy tick(s)",
    ]
    cause = describe(opened) if isinstance(opened, dict) else ""
    if cause:
        parts.append(cause)
    return "; ".join(parts)
