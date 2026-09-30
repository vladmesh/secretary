"""The scoped head lifecycle across launch, cgroup verification, and exit."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..memory import (
    MemoryScopeError,
    ScopeEvidence,
    head_loss_reason,
    memory_events,
    own_cgroup,
    scope_argv,
    scope_unit,
    supervisor_oom_protected,
)


@dataclass(frozen=True)
class ScopedHeadLifecycle:
    """The scoped head's launch, cgroup proof, and exit classification across processes."""

    run_id: str
    limit_mib: int
    owner_unit: str = ""

    def launcher_argv(
        self, supervisor: list[str], *, run_dir: Path, log_path: Path, timeout: float,
        pythonpath: str,
    ) -> list[str]:
        if "--daemonize" in supervisor:
            raise ValueError("a scoped supervisor must remain attached until its head exits")
        return [
            sys.executable, "-P", "-m", "secretary.runtime.head.local_pty.scope_launcher",
            str(run_dir), str(log_path), str(timeout),
            *scope_argv(self.run_id, self.limit_mib, supervisor,
                        pythonpath=pythonpath, owner_unit=self.owner_unit),
        ]

    @staticmethod
    def launch_until_started(arguments: list[str]) -> int:
        """Detach a synchronous systemd-run only after a durable start or a named refusal."""
        from . import protocol
        from .journal import RUN_STARTED, read_events

        run_dir, log_path, timeout = Path(arguments[0]), Path(arguments[1]), float(arguments[2])
        with log_path.open("ab", buffering=0) as log:
            scope = subprocess.Popen(
                arguments[3:], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(event.get("kind") == RUN_STARTED for event in read_events(run_dir / protocol.JOURNAL_NAME).events):
                return 0
            if (run_dir / protocol.STARTUP_ERROR_NAME).exists():
                return 0
            status = scope.poll()
            if status is not None:
                return status
            time.sleep(0.05)
        return 0  # The caller retains its own bounded startup check and log diagnostic.

    @staticmethod
    def started_or_exited(events: Sequence[dict[str, Any]], since: int) -> tuple[dict[str, Any], bool] | None:
        """A completed short head is still a successful launch with a durable exit to read."""
        from .journal import RUN_EXITED, RUN_STARTED

        fresh = events[since:]
        started = [event for event in fresh if event.get("kind") == RUN_STARTED]
        if not started:
            return None
        record = started[-1]
        exited = any(
            event.get("kind") == RUN_EXITED and int(event.get("seq") or 0) > int(record.get("seq") or 0)
            for event in fresh
        )
        return record, exited

    def verify_self(self) -> ScopeEvidence:
        """Run before `run.started`; a supervisor outside its configured scope refuses launch."""
        cgroup = own_cgroup()
        expected = str(self.limit_mib * 1024 * 1024)
        try:
            actual = (cgroup / "memory.max").read_text(encoding="ascii").strip() if cgroup else ""
            swap_max = (cgroup / "memory.swap.max").read_text(encoding="ascii").strip() if cgroup else ""
            oom_group = (cgroup / "memory.oom.group").read_text(encoding="ascii").strip() if cgroup else ""
        except OSError:
            actual = ""
            swap_max = ""
            oom_group = ""
        before = memory_events(cgroup)
        if (
            cgroup is None or cgroup.name != scope_unit(self.run_id)
            or actual != expected or swap_max != "0" or oom_group != "1" or before is None
            or not supervisor_oom_protected()
        ):
            raise MemoryScopeError(
                f"head scope {scope_unit(self.run_id)} did not materialize "
                f"MemoryMax={expected}, MemorySwapMax=0, memory.oom.group=1 and a protected supervisor"
            )
        return ScopeEvidence(cgroup, before)

    @staticmethod
    def exit_fields(status: int, evidence: ScopeEvidence, *, stopping: bool = False) -> dict[str, Any]:
        """A group OOM event kills the unprotected head; a stop never claims that event."""
        signal_number = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
        fields: dict[str, Any] = {
            "signal": signal_number,
            "exit_code": os.WEXITSTATUS(status) if os.WIFEXITED(status) else None,
        }
        reason = None if stopping else head_loss_reason(
            signal_number=signal_number, before=evidence.before, after=memory_events(evidence.cgroup),
        )
        if reason is not None:
            fields["head_loss_reason"] = reason
        return fields
