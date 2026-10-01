"""Leave a synchronous systemd scope running after its dispatcher caller exits.

The supervisor must stay as systemd-run's command for the scope's MemoryMax to
remain active. This short-lived launcher waits only until the supervisor has
started, then its systemd-run child is reparented and keeps the scope alive.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .scope_environment import EnvironmentTransferError, exec_scope
from .scoped_lifecycle import ScopedHeadLifecycle, launch_binding, launch_identity


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[0] == "--exec-gated":
        fd = int(args[1])
        released = os.read(fd, 1) == b"1"
        os.close(fd)
        if not released:
            return 1
        try:
            admitted = json.loads(args[2])
            command = args[3:]
            # Validate the actual caller argv, including the released scope emitter,
            # before privileged execution. An owner alone never supplies these fields.
            for option, key in (("--unit", "unit"), ("--run-id", "run_id"),
                                ("--role", "role"), ("--task", "task"),
                                ("--run-dir", "directory")):
                if command.count(option) != 1 or command[command.index(option) + 1] != admitted[key]:
                    raise ValueError("launch identity mismatch")
            cwd = command[command.index("--cwd") + 1] if "--cwd" in command else os.getcwd()
            if command.count("--cwd") > 1 or str(os.path.abspath(cwd)) != admitted["workspace"]:
                raise ValueError("launch workspace mismatch")
            admitted["launch_pid"] = os.getpid()
            admitted["launch_identity"] = launch_identity(os.getpid())
            binding = launch_binding(admitted, Path(admitted["directory"]))
            exec_scope(command, binding=binding)
            return 127  # exec must replace this process; returning cannot admit work.
        except (EnvironmentTransferError, OSError, ValueError, KeyError, TypeError, IndexError):
            print("scoped environment launch refused", file=sys.stderr)
            return 1
    return ScopedHeadLifecycle.launch_until_started(args)


if __name__ == "__main__":  # pragma: no cover - launched as a separate process
    raise SystemExit(main())
