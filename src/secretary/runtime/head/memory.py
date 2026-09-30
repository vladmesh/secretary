"""Per-head systemd scope limit and cgroup v2 OOM evidence."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

DEFAULT_MEMORY_LIMIT_MIB = 8192
MEMORY_LIMIT_REASON = "memory_limit"
CGROUP_ROOT = Path("/sys/fs/cgroup")


def memory_limit_mib(value: object, profile_id: str) -> int:
    """Accept a positive integer MiB value; bool is not an integer configuration value."""
    if type(value) is not int or not 1 <= value <= 1048576:
        raise ValueError(f"profile {profile_id!r} memory_limit_mib must be an integer from 1 to 1048576")
    return value


def scope_unit(run_id: str) -> str:
    # The hash also keeps arbitrary run ids out of a unit name.
    return f"secretary-head-{hashlib.sha256(run_id.encode()).hexdigest()[:24]}.scope"


def scope_argv(run_id: str, limit_mib: int, command: list[str], *, pythonpath: str = "") -> list[str]:
    """Register a system scope, then run its payload as the original runtime user."""
    groups = os.getgroups()
    group_option = f"--groups={','.join(str(group) for group in groups)}" if groups else "--clear-groups"
    return [
        "sudo", "-n", "-E", "systemd-run", "--system", "--scope", "--quiet",
        "--unit", scope_unit(run_id),
        f"--property=MemoryMax={limit_mib * 1024 * 1024}", "--",
        "setpriv", f"--reuid={os.getuid()}", f"--regid={os.getgid()}", group_option,
        *(["env", f"PYTHONPATH={pythonpath}"] if pythonpath else []),
        *command,
    ]


def own_cgroup() -> Path | None:
    """Return this process's unified cgroup, only if it is beneath the cgroup mount."""
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            path = (CGROUP_ROOT / line[3:].lstrip("/")).resolve()
            if path.is_relative_to(CGROUP_ROOT):
                return path
    return None


def memory_events(cgroup: Path | None) -> dict[str, int] | None:
    if cgroup is None:
        return None
    try:
        fields = (cgroup / "memory.events.local").read_text(encoding="ascii").splitlines()
        parsed = {parts[0]: int(parts[1]) for line in fields if len(parts := line.split()) == 2}
        return {key: parsed[key] for key in ("max", "oom_kill")}
    except (OSError, KeyError, ValueError):
        return None


def head_loss_reason(
    *, signal_number: int | None, before: dict[str, int] | None, after: dict[str, int] | None
) -> str | None:
    """A signal alone is never evidence of the cgroup's memory-limit kill."""
    if (
        signal_number == 9 and before is not None and after is not None
        and after["max"] > before["max"] and after["oom_kill"] > before["oom_kill"]
    ):
        return MEMORY_LIMIT_REASON
    return None
