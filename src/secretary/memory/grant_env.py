"""Render one launch-bound Memory bearer assignment for a scheduled head.

The triggered-agent runtime deliberately has no dependency on Secretary.  Its command renderer
calls this tiny Secretary-owned boundary while the head's shell is starting; the bearer never
passes through runtime.env and the service still verifies the durable HeadRun heartbeat.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from triggered_agents.runtime.head import HeadRun

from . import access


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--head-run", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args(argv)
    try:
        run = HeadRun.from_json(json.loads(args.head_run))
        subject = json.loads(args.subject)
        if not isinstance(subject, dict):
            raise ValueError("subject is not an object")
        # The shell heartbeat writer owns the file, while this launch helper owns the
        # private directory that makes its atomic publication possible.
        Path(run.pid_file).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        grant = access.issue_grant(run, subject, data_dir=args.data_dir)
    except (ValueError, TypeError, access.MemoryAccessError) as exc:
        parser.error(f"memory access binding could not be issued: {exc}")
    # This is consumed only by `env $(...) <head command>` at the launch boundary.
    print(f"{access.MEMORY_ACCESS_TOKEN_ENV}={grant.token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
