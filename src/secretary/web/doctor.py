"""The doctor lamp's reading: recorded installation health, classified by code, and cached.

Three colours and nothing else. Red when the installation cannot be trusted to run work -- or when
its health could not be read at all, which is the case a lamp must never draw as green; yellow when
it runs but somebody should look; green when health was read and reports no problem. The rule lives
in :data:`secretary.webproto.reads.PROBLEM_SEVERITY` beside the codes it classifies, and this module
only applies it and adds the one problem the summary cannot mint for itself: `health.unreadable`.

Recorded state only. The whole reading is one call of the read layer's `health_snapshot`, which is
`collect_status` over this host's own files -- systemd inventory, the production state, the
checkpoint snapshot, the store findings, the memory index. No `secretary doctor` is run, no SSH is
opened and no provider credential or endpoint is touched: this lamp is on every page, so a reading
that reached out would turn a page view into a remote call.

Cached for the same reason the provider layer is (:mod:`secretary.web.provider_usage`, whose shape
this copies): the bar is rendered by every page, and the collection behind it is not cheap, so one
in-process cache with its own TTL and an injectable clock decides how often it actually runs.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from secretary.webproto.errors import ReadError
from secretary.webproto.reads import lamp_colour, problem_severity

#: How long one health reading serves the lamp. Shorter than the provider window: this reading is
#: local, and an operator who repaired a unit should see the lamp change within about a minute.
CACHE_SECONDS = 60

#: The problem a reading that did not happen carries. It is a problem like any other, with a code
#: like any other, so that "health is unknown" is classified by the same table as everything else.
UNREADABLE_CODE = "health.unreadable"

#: Said when this web process was built without a doctor layer at all. Health is then unknown,
#: which is red -- a lamp that stayed green because nothing was wired would be the worst kind.
DOCTOR_NOT_BUILT = "this web process was built without the doctor layer"


class DoctorLayer:
    """One cached reading of recorded health, as the lamp and the doctor page both read it."""

    def __init__(
        self,
        read_health: Callable[[], dict[str, Any]],
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.read_health = read_health
        self.now = now
        self._cached: tuple[float, dict[str, Any]] | None = None

    def doctor_snapshot(self) -> dict[str, Any]:
        """The current colour and the problems behind it, collected at most once per window."""
        observed = self.now()
        if self._cached is not None and observed - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        document = self._collect()
        self._cached = (observed, document)
        return document

    def _collect(self) -> dict[str, Any]:
        try:
            snapshot = self.read_health()
        except ReadError as exc:
            return unreadable(exc.message)
        section = snapshot.get("health") if isinstance(snapshot, dict) else None
        section = section if isinstance(section, dict) else {}
        status = section.get("status")
        source = section.get("source") if isinstance(section.get("source"), dict) else None
        if not isinstance(status, dict) or not status:
            reason = str((source or {}).get("reason") or "installation health was not read")
            return unreadable(reason, source=source)
        problems = [
            {
                "code": str(finding.get("code") or ""),
                "message": str(finding.get("message") or ""),
                "severity": problem_severity(str(finding.get("code") or "")),
            }
            for finding in status.get("findings") or []
            if isinstance(finding, dict)
        ]
        return {
            "kind": "doctor",
            "observed_at": str(snapshot.get("observed_at") or "") or None,
            "readable": True,
            "reason": None,
            "colour": lamp_colour(problems),
            "problems": problems,
            "source": source,
        }


def unreadable(reason: str, *, source: dict[str, Any] | None = None) -> dict[str, Any]:
    """Health that could not be read, said as the problem it is rather than as an empty list."""
    problem = {
        "code": UNREADABLE_CODE,
        "message": f"this installation's health could not be read: {reason}",
        "severity": problem_severity(UNREADABLE_CODE),
    }
    return {
        "kind": "doctor",
        "observed_at": None,
        "readable": False,
        "reason": reason,
        "colour": lamp_colour([problem]),
        "problems": [problem],
        "source": source,
    }
