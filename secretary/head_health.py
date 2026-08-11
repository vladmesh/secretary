"""Cheap, cached preflight checks for dispatcher head resources."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from secretary._fsutil import write_json
from secretary.dispatcher_types import HostError


PROBE_TTL_SECONDS = 300
PROBE_TIMEOUT_SECONDS = 20
# A head named in a fallback chain that the registry no longer describes. It is a readiness status
# rather than a silent skip because it is the same kind of fact as a red resource — this head
# cannot be launched — and the tick has to be able to say so.
MISSING_HEAD = "missing"


@dataclass(frozen=True)
class HeadReadiness:
    resource: str
    status: str
    reason: str
    checked_at: float
    cached: bool = False

    @property
    def launch_allowed(self) -> bool:
        return self.status in {"ready", "unknown"}

    def to_json(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "status": self.status,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "cached": self.cached,
        }


@dataclass(frozen=True)
class HeadChoice:
    """Which head a role is actually launched on, once resource health has had its say.

    ``head`` is empty when nothing reachable from ``preferred`` can be launched — that is the
    claim-skip: the card stays in Ready and no head is put into a dead resource. ``rejected``
    carries every candidate the walk turned down, in the order it read them, so the tick can name
    which resource is dead and why rather than only reporting that nothing was claimed.
    """

    preferred: str
    head: str
    readiness: HeadReadiness
    rejected: tuple[tuple[str, HeadReadiness], ...] = ()

    @property
    def resolved(self) -> bool:
        return bool(self.head)

    @property
    def substituted(self) -> bool:
        """Whether the launch is on a head the card did not ask for."""
        return bool(self.head) and self.head != self.preferred

    @property
    def reason(self) -> str:
        """One line for the tick: why this is the head, or why there is none.

        A preferred head with no chain behind it reads exactly as it did before there were chains —
        the resource's own reason and nothing else — because that is the whole story there. The
        chain is spelled out only when one was actually walked, so the line grows only where this
        card added something to say.
        """
        if not self.head and len(self.rejected) < 2:
            return self.readiness.reason
        rejected = "; ".join(
            f"{head} on {readiness.resource or '(no resource)'} is {readiness.status}"
            f" ({readiness.reason})"
            for head, readiness in self.rejected
        )
        if not self.head:
            return f"no launchable head for {self.preferred}: {rejected}"
        if not self.substituted:
            return self.readiness.reason
        return (
            f"head {self.preferred} is not launchable ({rejected}); "
            f"falling back to {self.head} on {self.readiness.resource}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "preferred": self.preferred,
            "head": self.head,
            "substituted": self.substituted,
            "readiness": self.readiness.to_json(),
            "rejected": [
                dict(readiness.to_json(), head=head) for head, readiness in self.rejected
            ],
            "reason": self.reason,
        }


def resolve_head_chain(
    preferred: str,
    readiness_of: Callable[[str], HeadReadiness],
    fallback_of: Callable[[str], Sequence[str] | None],
) -> HeadChoice:
    """The first launchable head at or below ``preferred``, breadth-first over the fallback chains.

    A red or spent resource is a property of the account, not of the profile drawing on it, so the
    answer to "the preferred head cannot run" is another head on a *different* resource — the
    chain the canon writes down, and only that chain. Nothing is inferred: a head with no chain
    and a dead resource is a claim-skip, which is the point (a card waiting in Ready costs
    nothing; a head launched into a spent subscription costs an attempt and a round).

    ``fallback_of`` returns None for a head the registry does not describe, and a *chain entry*
    that answers None is dropped without ever reaching ``readiness_of``. Existence is asked here
    because it cannot be asked there: a readiness check reads the profile to find the resource to
    probe, so a head that is not in the registry either raises out of that call (the dispatcher's
    catalog) or answers about nothing at all. Neither is a verdict a claim may act on, and the
    chain is exactly where a deleted profile survives — the registry validates chain targets when
    it is loaded, so what reaches here is whatever a later edit left behind. ``preferred`` itself
    is not filtered that way: whoever chose it (a card override, a role default) resolves it
    against the registry before asking, so a second check here with different manners would answer
    a question that caller has already answered, and answer it more quietly. Chains may be cyclic —
    the codex heads name the claude ones and back — so every candidate is read once.
    """
    seen: set[str] = set()
    rejected: list[tuple[str, HeadReadiness]] = []
    queue = [preferred]
    while queue:
        candidate = queue.pop(0)
        if candidate in seen:
            continue
        seen.add(candidate)
        chain = fallback_of(candidate)
        if chain is None and candidate != preferred:
            rejected.append((candidate, HeadReadiness(
                "", MISSING_HEAD, f"head {candidate} is not in the registry", time.time())))
            continue
        readiness = readiness_of(candidate)
        if readiness.launch_allowed:
            return HeadChoice(preferred, candidate, readiness, tuple(rejected))
        rejected.append((candidate, readiness))
        queue.extend(chain or ())
    first = rejected[0][1] if rejected else HeadReadiness(
        "", MISSING_HEAD, f"head {preferred} is not in the registry", time.time())
    return HeadChoice(preferred, "", first, tuple(rejected))


class HeadHealth:
    """Store resource verdicts independently from the dispatcher attempt state.

    A failed probe is not proof that the provider is down.  Only a definite authentication or
    provider failure stops a launch; probe execution failures are recorded as ``unknown`` and
    retry after the normal TTL.
    """

    def __init__(self, catalog: Any, data_dir: Path) -> None:
        self.catalog = catalog
        self.path = data_dir / "dispatcher" / "resource_health.json"

    def check(self, head: str) -> HeadReadiness:
        try:
            profile = self.catalog.head_profile(head)
            resource = str(profile["resource"])
            probe = str(self.catalog.resource(resource).get("probe") or "")
        # HostError is how the catalog says "no such head"; a health probe answers that the same
        # way it answers every other unreadable configuration — unknown, not a crash on the
        # claim-time walk that is only asking whether this candidate is usable.
        except (AttributeError, HostError, KeyError, TypeError, ValueError) as exc:
            return HeadReadiness("", "unknown", f"head health configuration unavailable: {type(exc).__name__}", time.time())
        if not probe:
            return HeadReadiness(resource, "unknown", "resource has no probe command", time.time())

        cache = self._load()
        entry = cache.get(resource)
        now = time.time()
        if isinstance(entry, dict) and now - float(entry.get("checked_at") or 0) < PROBE_TTL_SECONDS:
            return HeadReadiness(resource, str(entry.get("status") or "unknown"), str(entry.get("reason") or ""), float(entry["checked_at"]), True)

        verdict = self._run(resource, probe, now)
        cache[resource] = verdict.to_json()
        try:
            self._save(cache)
        except RuntimeError:
            # The preflight still has a useful verdict when its observability cache cannot be
            # written.  A later dispatcher write will surface a broader data-dir failure.
            pass
        return verdict

    def snapshot(self) -> dict[str, Any]:
        return self._load()

    def _run(self, resource: str, probe: str, now: float) -> HeadReadiness:
        try:
            completed = subprocess.run(
                probe, shell=True, text=True, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return HeadReadiness(resource, "unknown", "probe timed out", now)
        except Exception as exc:  # a broken probe must not turn into a false resource outage
            return HeadReadiness(resource, "unknown", f"probe could not run: {type(exc).__name__}", now)
        if completed.returncode == 0:
            return HeadReadiness(resource, "ready", "probe succeeded", now)
        text = " ".join((completed.stdout or "", completed.stderr or "")).lower()
        if any(marker in text for marker in ("login", "not authenticated", "unauthorized", "authentication", " 401", " 403")):
            return HeadReadiness(resource, "unauthenticated", "resource authentication failed", now)
        # A spent subscription answers in its own words, and none of them is "rate limit": codex
        # says "You've hit your usage limit … purchase more credits or try again at <date>".
        # Classified before the provider-unavailable markers because the two read differently to an
        # operator — this resource is not flaky, it is out until the quota resets — and because
        # leaving it unclassified made it `unknown`, which `launch_allowed` treats as usable. On
        # 2026-08-06 that cost sprint:1200 two launches and a round into a dead resource before the
        # watchdog ceiling stopped it.
        if any(marker in text for marker in ("usage limit", "quota", "credits", "insufficient_quota", "billing")):
            return HeadReadiness(resource, "exhausted", "resource quota is spent", now)
        if any(marker in text for marker in ("503", "circuit_open", "unavailable", "rate limit", " 429", "connection", "network")):
            return HeadReadiness(resource, "unavailable", "resource provider is unavailable", now)
        return HeadReadiness(resource, "unknown", "probe returned an unclassified failure", now)

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self, cache: dict[str, Any]) -> None:
        write_json(self.path, cache)
