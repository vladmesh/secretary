"""Scheduled maintenance of the private instance repository.

Git's implicit ``gc --auto`` would otherwise run inside whichever checkpoint ``git commit`` first
crosses its loose-object threshold, so a dispatcher tick could spend minutes packing a
multi-gigabyte repository.  The lifecycle turns that off (``gc.auto=0`` and ``maintenance.auto=false``
in :data:`secretary.state_repo.PACKING_CONTROLS`), and this module is what runs instead, from the
``instance-maintenance`` timer and never from a tick.

The run keeps Git's own heuristic, only moved out of the tick: ``gc --auto`` with the stock
thresholds does nothing on a quiet day, packs loose objects incrementally once there are more than
:data:`AUTO_LOOSE_OBJECTS`, and consolidates packs once there are more than :data:`AUTO_PACK_LIMIT`.

Coordination with the checkpoint writer and pusher: the packing step never takes
:func:`secretary.state_repo.state_repo_lock`.  It touches objects only — Git keeps a concurrent
commit's new objects (loose objects younger than ``gc.pruneExpire`` survive) and a repack replaces
packs before deleting them — and the two ref-writing parts of ``gc`` (``pack-refs`` and reflog
expiry) are switched off for it, so a commit's branch update never meets a ref lock held by
``gc``.  Reflogs are then expired as a separate short step under the state-repo lock; that is the
only moment a tick can wait on maintenance, bounded by :data:`REFLOG_TIMEOUT_SECONDS`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from secretary import state_repo

# Git's stock `gc --auto` thresholds, restated because the repository's own `gc.auto` is 0.
AUTO_LOOSE_OBJECTS = 6700
AUTO_PACK_LIMIT = 50
# A full consolidation of a multi-gigabyte repository with `pack.threads=1` is slow, and nothing
# waits on it; the bound only keeps a wedged run from living until the next trigger.
GC_TIMEOUT_SECONDS = 4 * 60 * 60
REFLOG_TIMEOUT_SECONDS = 60

GC_OVERRIDES = (
    ("gc.auto", str(AUTO_LOOSE_OBJECTS)),
    ("gc.autoPackLimit", str(AUTO_PACK_LIMIT)),
    # A detached gc would outlive the oneshot unit and be killed with its control group.
    ("gc.autoDetach", "false"),
    # Neither ref-writing step may run outside the state-repo lock; see the module docstring.
    ("gc.packRefs", "false"),
    ("gc.reflogExpire", "never"),
    ("gc.reflogExpireUnreachable", "never"),
)


def gc_command() -> list[str]:
    """The Git arguments of the packing step, without the instance-repository prefix."""
    overrides = [argument for key, value in GC_OVERRIDES for argument in ("-c", f"{key}={value}")]
    return [*overrides, "gc", "--auto", "--quiet"]


def count_objects(instance_dir: Path) -> dict[str, int]:
    """``git count-objects -v`` as integers: ``count`` is the loose objects, ``packs`` the packs."""
    output = state_repo.git(instance_dir, ["count-objects", "-v"], label="count instance objects")
    counts: dict[str, int] = {}
    for line in output.splitlines():
        key, _, value = line.partition(":")
        try:
            counts[key.strip()] = int(value.strip())
        except ValueError:
            continue
    return counts


def run(instance_dir: Path) -> dict[str, Any]:
    """One maintenance run. Raises :class:`secretary.state_repo.StateRepoError` on a Git failure."""
    instance = state_repo.require_repo(instance_dir)
    started = time.monotonic()
    before = count_objects(instance)
    state_repo.git(instance, gc_command(), label="instance gc", timeout=GC_TIMEOUT_SECONDS)
    with state_repo.state_repo_lock(instance):
        state_repo.git(
            instance,
            ["reflog", "expire", "--all"],
            label="instance reflog expire",
            timeout=REFLOG_TIMEOUT_SECONDS,
        )
    after = count_objects(instance)
    return {
        "instance": str(instance),
        "loose_objects": {"before": before.get("count"), "after": after.get("count")},
        "packs": {"before": before.get("packs"), "after": after.get("packs")},
        "duration_s": round(time.monotonic() - started, 3),
    }
