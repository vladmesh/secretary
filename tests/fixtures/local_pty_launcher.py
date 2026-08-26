"""A stand-in for the dispatcher tick that starts a head and then goes away.

It exists because the two properties it is used to prove cannot be proven from inside the test
process: a signal sent to the launcher's *process group* must not reach the head, and the
supervisor must still be there once the launcher itself has exited. Both need a launcher whose
group and lifetime are not the test runner's.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from triggered_agents.runtime.head.local_pty.client import spawn_head


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--handle-file", required=True)
    parser.add_argument("--linger", type=float, default=0.0)
    args = parser.parse_args()
    handle = spawn_head(
        root=args.root,
        run_id=args.run_id,
        role="worker",
        task="secretary-1463",
        command=args.command,
        quiet_seconds=0.4,
    )
    record = {key: str(value) for key, value in asdict(handle).items()}
    record["supervisor_pid"] = handle.supervisor_pid
    record["head_pid"] = handle.head_pid
    record["launcher_pid"] = os.getpid()
    Path(args.handle_file).write_text(json.dumps(record), encoding="utf-8")
    if args.linger:
        time.sleep(args.linger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
