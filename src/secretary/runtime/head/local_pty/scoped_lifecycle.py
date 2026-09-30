"""The scoped head lifecycle across launch, cgroup verification, and exit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..memory import (
    CGROUP_ROOT,
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

    def persist(self, run_dir: Path) -> None:
        """Leave the scope name on disk before any process can create the unit."""
        path = run_dir / "scope-owner.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump({"run_id": self.run_id, "unit": scope_unit(self.run_id)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def from_run_dir(run_dir: Path) -> ScopedHeadLifecycle | None:
        try:
            record = json.loads((run_dir / "scope-owner.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise MemoryScopeError(f"could not read scope owner in {run_dir}: {exc}") from exc
        try:
            run_id = record["run_id"]
            if not isinstance(run_id, str) or record["unit"] != scope_unit(run_id):
                raise ValueError("invalid scope identity")
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryScopeError(f"invalid scope owner in {run_dir}: {exc}") from exc
        return ScopedHeadLifecycle(run_id, 1)

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
                try:
                    scope.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    scope.terminate()
                    try:
                        scope.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        scope.kill()
                        scope.wait()
                return 0
            status = scope.poll()
            if status is not None:
                return status
            time.sleep(0.05)
        scope.terminate()
        try:
            scope.wait(timeout=5)
        except subprocess.TimeoutExpired:
            scope.kill()
            scope.wait()
        return 1

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

    def cancel_started(self, *, socket_path: Path, journal_path: Path, started_seq: int) -> None:
        """Stop a launched head and prove its whole scope empty before launch fails.

        The heartbeat can lag run.started. In that interval the caller has no handle, but
        the supervisor already owns a live head. Its socket requests a clean stop;
        systemd stops the whole scope even if the head was reaped or the socket failed.
        """
        from .client import SupervisorClient
        from .journal import RUN_EXITED, read_events

        def reaped() -> bool:
            return any(
                event.get("kind") == RUN_EXITED and int(event.get("seq") or 0) > started_seq
                for event in read_events(journal_path).events
            )

        requested = False
        try:
            with SupervisorClient.connect(socket_path, timeout=1.0) as client:
                client.stop(initiator="startup_failed", signal_name="KILL")
                requested = True
        except (OSError, RuntimeError):
            pass
        deadline = time.monotonic() + (10.0 if requested else 0.0)
        while time.monotonic() < deadline:
            if reaped():
                break
            time.sleep(0.05)
        self.stop_and_prove_empty()

    def stop_and_prove_empty(self) -> None:
        """Stop the entire cgroup and keep ownership if empty membership cannot be proved."""
        cgroup = CGROUP_ROOT / "system.slice" / scope_unit(self.run_id)
        if not cgroup.exists():
            return
        try:
            stopped = subprocess.run(
                ["sudo", "-n", "systemctl", "stop", scope_unit(self.run_id)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                check=False, timeout=15.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MemoryScopeError(f"could not stop head scope {scope_unit(self.run_id)}: {exc}") from exc
        if stopped.returncode != 0:
            detail = stopped.stderr.decode("utf-8", errors="replace").strip()
            raise MemoryScopeError(f"could not stop head scope {scope_unit(self.run_id)}: {detail}")
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fields = (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
            except FileNotFoundError:
                return  # systemd removed the cgroup after its final member left.
            except OSError as exc:
                raise MemoryScopeError(f"could not verify empty head scope {scope_unit(self.run_id)}: {exc}") from exc
            if "populated 0" in fields:
                return  # cgroup.events counts descendants, including separate process groups.
            if time.monotonic() >= deadline:
                raise MemoryScopeError(f"head scope {scope_unit(self.run_id)} still has members")
            time.sleep(0.05)

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
    def exit_fields(
        status: int, evidence: ScopeEvidence, *, stopping: bool = False,
        events_at_head_exit: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """A group OOM event kills the unprotected head; a stop never claims that event."""
        signal_number = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
        fields: dict[str, Any] = {
            "signal": signal_number,
            "exit_code": os.WEXITSTATUS(status) if os.WIFEXITED(status) else None,
        }
        reason = None if stopping else head_loss_reason(
            signal_number=signal_number, before=evidence.before, after=events_at_head_exit,
            group_kill_at_head_exit=events_at_head_exit is not None,
        )
        if reason is not None:
            fields["head_loss_reason"] = reason
        return fields
