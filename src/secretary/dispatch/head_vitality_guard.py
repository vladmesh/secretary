"""The single destructive guard for the wait/watchdog path (card S1-4).

Every watchdog-driven decision to stop, kill, respawn or replace a worker/review head
funnels through :func:`assert_destructive_allowed` *after* the persisted
``VitalityEpisode`` has spoken. This is the first production consumer of the
head-vitality ladder, and it encodes the plan's asymmetry of cost: a false "working"
costs an idle hour, a false kill loses a live round. The guard is the last fence between
a verdict and a destructive action, so it trusts only what the reducer earned:

  * ``HealthyActive``, ``HealthyQuiet``, ``Unverifiable`` and ``Suspended`` are refused --
    a possibly-working, quiet-below-threshold, unobservable or merely SIGSTOPed head is
    never touched. Suspension in particular is SIGCONT territory (S1-5), not kill
    territory.
  * A missing episode is refused: a destructive step nobody observed is a step taken on
    memory nobody wrote. The fail-safe direction of every anomaly here is ``wait`` with
    telemetry, never a silent no-op and never a kill.
  * An episode naming another ``HeadRun`` is refused: after a respawn the record briefly
    still carries the dead run's history, and acting on it would let one run's stall
    verdict execute against its replacement.
  * ``SuspectedStall`` never authorises destruction here either; policy gives it at most
    one idempotent nudge, which the caller takes before ever asking this guard about a
    destructive step. Reaching the guard with a suspicion is therefore refused too --
    belt and braces, in case a future call site wires the ladder wrong.
  * Only ``ConfirmedStall`` and ``Dead`` pass.

One belt-and-braces rule narrows the first production release further. The reducer's
pid-only aging arm (issue 656: bare process existence is not liveness) can confirm a
stall for a head whose provider source *never answered at all*. Confirmation there rests
on one channel only, so the guard additionally requires that the role's ordinary outer
ceiling (the ``WORKER_REPORT_STALL_DEFAULT`` class, passed in by the caller) has also
elapsed since the episode began accumulating evidence. A confirmed pid-only stall is
therefore acted on strictly later than the old clock-only machinery would have acted,
never earlier. Episodes that earned confirmation on witnessed strong quiet (a provider
cursor that answered and is still answering) are not held back: that is the two-channel
evidence the whole sprint exists to listen for. A source that answered and has since gone
dark is held like a pid-only one (secretary-1543): the reducer now ages such an episode
past ``dark_ceiling`` instead of freezing it, so the confirmation behind the destructive
step again rests on one channel.

Like the rest of ``secretary.dispatch.head_vitality*`` this module is pure: it reads no
clock, no file and no host, performs no I/O, and raises nothing. The caller owns ``now``,
supplies the episode from the record, and turns a refusal into ``wait`` plus one
idempotent telemetry comment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from secretary.dispatch.head_vitality_episode import (
    VitalityEpisode,
    VitalityVerdict,
)

#: The Progress-axis source names, mirrored from the reducer's vocabulary. Kept as plain
#: strings so the guard does not have to import snapshot enums to ask one structural
#: question: did any progress source ever answer for this episode?
_PROGRESS_SOURCE_NAMES = frozenset({"provider_cursor"})


class GuardRefusal(StrEnum):
    """Why a destructive step was refused, in the vocabulary operators read."""

    MISSING_EPISODE = "missing-episode"
    FOREIGN_RUN = "foreign-run"
    HEALTHY_ACTIVE = "healthy-active"
    HEALTHY_QUIET = "healthy-quiet"
    SUSPENDED = "suspended"
    RETAINED = "retained"
    UNVERIFIABLE = "unverifiable"
    SUSPECTED_STALL = "suspected-stall"
    PID_ONLY_CEILING_UNELAPSED = "pid-only-ceiling-unelapsed"


#: Verdicts that may never ground a destructive step, each mapped to its refusal class
#: and the operator-facing why. Spelled out per verdict so a new ladder rung added later
#: fails here loudly instead of silently falling through to allowed.
_REFUSED_VERDICTS: dict[VitalityVerdict, tuple[GuardRefusal, str]] = {
    VitalityVerdict.HEALTHY_ACTIVE: (
        GuardRefusal.HEALTHY_ACTIVE,
        "the head is advancing right now",
    ),
    VitalityVerdict.HEALTHY_QUIET: (
        GuardRefusal.HEALTHY_QUIET,
        "the head is alive and quiet below every stall threshold",
    ),
    VitalityVerdict.UNVERIFIABLE: (
        GuardRefusal.UNVERIFIABLE,
        "no strong channel answered; nothing is concluded",
    ),
    VitalityVerdict.SUSPENDED: (
        GuardRefusal.SUSPENDED,
        "the process is parked on a stop signal: SIGCONT territory, not kill territory",
    ),
    VitalityVerdict.RETAINED: (
        GuardRefusal.RETAINED,
        "the dispatcher's own confirmed retention is holding this process: not a head to recover",
    ),
    VitalityVerdict.SUSPECTED_STALL: (
        GuardRefusal.SUSPECTED_STALL,
        "suspicion authorises one idempotent nudge, never destruction",
    ),
}


@dataclass(frozen=True)
class GuardDecision:
    """One guard ruling: allowed, or refused with the reason an operator can audit."""

    action: str
    allowed: bool
    refusal: GuardRefusal | None = None
    reason: str = ""
    verdict: str = ""
    episode_run_id: str = ""

    def to_json(self) -> dict[str, Any]:
        """The bounded form that travels in tick outcomes and telemetry comments."""
        return {
            "action": self.action,
            "allowed": self.allowed,
            "refusal": self.refusal.value if self.refusal is not None else "",
            "reason": self.reason,
            "verdict": self.verdict,
            "episode_run_id": self.episode_run_id,
        }


def _progress_source_answering(episode: VitalityEpisode) -> bool:
    """Whether a progress source both ANSWERED for this episode and is still answering.

    Witnessing is an answered channel, not a tracked one: a cursor on file means the
    provider spoke and its later silence is a frozen outage. Merely being recorded dark
    (``unavailable_since``) is not witnessing -- a provider that was never heard from
    leaves the pid as the only witness, and issue 656's contract applies: the pid's own
    sustained silence about progress is allowed to age, which is exactly how such
    episodes can reach ``ConfirmedStall`` and why the outer-ceiling rule below exists
    for them.

    Since secretary-1543 a source that answered and has since gone dark is treated the
    same way here. The reducer no longer freezes such an episode indefinitely -- past
    ``dark_ceiling`` it ages on the pid alone, which is the fix -- so at the moment of a
    destructive step the evidence behind that confirmation is again one channel, and it
    earns the same outer-ceiling hold. Two-channel evidence means a progress source that
    is answering NOW, not one that answered once.
    """
    witnessed = bool(set(episode.evidence_cursors) & _PROGRESS_SOURCE_NAMES) or episode.last_progress_at > 0.0
    dark = bool(set(episode.unavailable_since) & _PROGRESS_SOURCE_NAMES)
    return witnessed and not dark


def _refused(
    action: str,
    refusal: GuardRefusal,
    reason: str,
    episode: VitalityEpisode | None = None,
) -> GuardDecision:
    verdict = getattr(getattr(episode, "verdict", None), "value", "")
    return GuardDecision(
        action=action,
        allowed=False,
        refusal=refusal,
        reason=reason,
        verdict=verdict,
        episode_run_id="" if episode is None else episode.run_id,
    )


def assert_destructive_allowed(
    episode: Any,
    action: str,
    now: float,
    *,
    current_run_id: str = "",
    pid_only_outer_ceiling_seconds: float = 0.0,
) -> GuardDecision:
    """Rule on one destructive step against the run's persisted vitality verdict.

    ``episode`` is the record's persisted ``VitalityEpisode`` (``None`` when this run was
    never observed). ``action`` names the step for telemetry (``"worker-respawn"``,
    ``"review-respawn"``, ``"worker-escalate"`` ...). ``current_run_id`` is the run the
    caller believes it is acting on; when both it and the episode name runs, a mismatch
    refuses. ``pid_only_outer_ceiling_seconds`` is the role's ordinary outer stall
    ceiling, applied only to pid-only-earned confirmations as described in the module
    docstring.

    The function never raises: an episode that is not an episode is a missing episode,
    and a malformed ``now`` would make every comparison meaningless, so it refuses like
    any other unknown.
    """
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        return _refused(
            action,
            GuardRefusal.MISSING_EPISODE,
            "the guard was handed a clock it cannot read; waiting is the safe side",
        )
    now = float(now)
    if not isinstance(episode, VitalityEpisode):
        return _refused(
            action,
            GuardRefusal.MISSING_EPISODE,
            "no vitality episode is on file; a destructive step nobody observed "
            "would act on nobody's evidence",
        )
    if current_run_id and episode.run_id != str(current_run_id):
        return _refused(
            action,
            GuardRefusal.FOREIGN_RUN,
            f"the persisted episode names run {episode.run_id}, not the current run "
            f"{current_run_id}: one run's stall verdict must never execute against "
            "its replacement",
            episode,
        )
    basis = ", ".join(episode.basis) if episode.basis else "none"
    if episode.verdict in _REFUSED_VERDICTS:
        refusal, why = _REFUSED_VERDICTS[episode.verdict]
        return _refused(
            action,
            refusal,
            f"{why} (basis: {basis})",
            episode,
        )
    if episode.verdict is not VitalityVerdict.CONFIRMED_STALL and episode.verdict is not VitalityVerdict.DEAD:
        # A ladder rung this card does not know -- or a malformed verdict that never was
        # one: refuse on principle. New verdicts must be added to the map above
        # explicitly before they can destroy anything.
        raw = getattr(episode.verdict, "value", episode.verdict)
        return _refused(
            action,
            GuardRefusal.UNVERIFIABLE,
            f"verdict {raw} is not one the guard knows as destructive-authorising; refusing on principle",
            episode,
        )
    if (
        episode.verdict is VitalityVerdict.CONFIRMED_STALL
        and not _progress_source_answering(episode)
        and pid_only_outer_ceiling_seconds > 0
    ):
        elapsed = max(0.0, now - episode.started_at)
        if elapsed < pid_only_outer_ceiling_seconds:
            dark = sorted(set(episode.unavailable_since) & _PROGRESS_SOURCE_NAMES)
            earned_on = (
                f"with {', '.join(dark)} dark (secretary-1543 arm)"
                if dark
                else "on the pid alone (issue 656 arm)"
            )
            return _refused(
                action,
                GuardRefusal.PID_ONLY_CEILING_UNELAPSED,
                f"confirmation was earned {earned_on}, so the "
                f"outer ceiling of {int(pid_only_outer_ceiling_seconds)}s must also "
                f"have elapsed ({int(elapsed)}s have)",
                episode,
            )
    return GuardDecision(
        action=action,
        allowed=True,
        verdict=episode.verdict.value,
        episode_run_id=episode.run_id,
        reason=(f"{episode.verdict.value} stands (basis: {basis})"),
    )
