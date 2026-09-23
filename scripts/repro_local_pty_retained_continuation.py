"""Reproduce a red-verdict continuation into a retained local-pty Claude head (secretary-1702).

Raises one Claude head of its own under the product's `LocalPtyHeadRuntime`, in a temporary run
root and workspace, lets it finish one trivial turn, then does to it what the dispatcher does to a
retained worker and to its continuation:

  * `--mode retained` (the default): `SIGSTOP` to the head's process group, as
    `CommandHostRuntime.retain_worker` does, then `deliver` a `TASK.md` pointer with a transport
    whose `before_send` sends `SIGCONT`, as `CommandHostRuntime.resume_worker` does;
  * `--mode idle`: the same continuation into the same idle, never-suspended session, which
    separates "a session that has completed turns" from "a session that was suspended".

It prints the head's screen (rendered from `read_output`) before and after the delivery, the
receipt, the journal records of the delivery, and whether Claude's own transcript holds the
continuation as a user message. Everything it touches it created: the head is stopped and the
temporary directories removed on the way out. It costs one short Claude session (two turns at
most) on `--model`, so it is not part of any suite.

    python scripts/repro_local_pty_retained_continuation.py [--mode retained|idle] [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from secretary.dispatch.watchdog import head_process_status
from secretary.runtime.claude_env import ensure_trust
from secretary.runtime.head.local_pty import SupervisorClient, read_events
from secretary.runtime.head.operations import NudgePointer
from secretary.runtime.head.run import StopInitiator
from secretary.runtime.head.spec import HeadSpec
from secretary.runtime.head.task_ref import TaskRef
from secretary.runtime.local_pty_head import LocalPtyHeadRuntime

ROWS, COLS = 40, 120
CLAUDE = HeadSpec(profile_id="claude-repro", adapter="claude", effort="default")


@dataclass(frozen=True)
class Transport:
    """The one field of the dispatcher's transport a continuation relies on."""

    before_send: Callable[[], Any] | None = None


def screen(socket_path: Path) -> str:
    """The head's terminal as a person would see it, from the supervisor's output tail."""
    with SupervisorClient.connect(socket_path, timeout=5.0) as client:
        data = client.read_output().get("bytes_data") or b""
    try:
        import pyte  # type: ignore[import-not-found]
    except ImportError:
        text = re.sub(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-B]|\x1b[=>]", b"", data)
        return "\n".join(text.decode("utf-8", "replace").splitlines()[-ROWS:])
    term = pyte.Screen(COLS, ROWS)
    pyte.ByteStream(term).feed(data)
    return "\n".join(line.rstrip() for line in term.display).strip("\n")


def status(socket_path: Path) -> dict[str, Any]:
    with SupervisorClient.connect(socket_path, timeout=5.0) as client:
        return client.status()


def await_quiet(socket_path: Path, *, quiet: float = 6.0, bound: float = 180.0) -> None:
    """Until the head's turn has closed and it has printed nothing for `quiet` seconds."""
    deadline = time.monotonic() + bound
    last, since = -1, time.monotonic()
    while time.monotonic() < deadline:
        seen = status(socket_path)
        printed = int(seen.get("output_bytes") or 0)
        if printed != last or seen.get("turn_open"):
            last, since = printed, time.monotonic()
        elif time.monotonic() - since >= quiet:
            return
        time.sleep(0.5)
    raise SystemExit("the head never went quiet")


def signal_group(pid_file: str, number: int) -> None:
    """`CommandHostRuntime._signal_head`, for this script's own head only."""
    pid = int(head_process_status(pid_file)["pid"])
    group = os.getpgid(pid)
    if group != os.getpgrp():
        os.killpg(group, number)
    else:
        os.kill(pid, number)


def transcript_user_messages(workspace: Path) -> list[str]:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    found: list[str] = []
    for path in sorted((Path.home() / ".claude" / "projects" / slug).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "user":
                continue
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, str):
                found.append(content)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=("retained", "idle"), default="retained")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--keep", action="store_true", help="leave the temporary directories behind")
    args = parser.parse_args()

    base = Path(tempfile.mkdtemp(prefix="secretary-1702-repro-"))
    root, workspace = base / "heads", base / "workspace"
    root.mkdir()
    workspace.mkdir()
    # What the launcher does for every Claude head's workspace, or the folder-trust dialog holds it.
    ensure_trust(Path.home() / ".claude.json", str(workspace))
    runtime = LocalPtyHeadRuntime(root, head_process_status=head_process_status)
    command = shlex.join(
        [
            "claude",
            "--dangerously-skip-permissions",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--model",
            args.model,
        ]
    )
    run = None
    try:
        print(f"== raising {command} in {workspace}", flush=True)
        receipt = runtime.start(
            CLAUDE,
            str(workspace),
            TaskRef.card("secretary-1702", document=str(workspace / "TASK.md")),
            command=command,
            title="secretary-1702 repro",
            pointer=NudgePointer.line("Reply with the single word: ready"),
            transport=Transport(),
            subject="head-launch",
            role="worker",
            rows=ROWS,
            cols=COLS,
        )
        print(f"launch: status={receipt.status} reason={receipt.reason!r}", flush=True)
        if not receipt.ok:
            return 2
        run = receipt.run
        socket_path = Path(run.handle)
        await_quiet(socket_path)
        (workspace / "TASK.md").write_text(
            "# Task\n\nReply with the single word: continued\n", encoding="utf-8"
        )
        pointer = NudgePointer.at_document(
            str(workspace / "TASK.md"), "Generation 2: use its report command, not an earlier turn's."
        )
        activate = None
        if args.mode == "retained":
            signal_group(run.pid_file, signal.SIGSTOP)
            time.sleep(1.0)
            print(f"retained: head stopped={head_process_status(run.pid_file).get('stopped')}", flush=True)

            def activate() -> None:
                print("before_send: SIGCONT", flush=True)
                signal_group(run.pid_file, signal.SIGCONT)

        floor = len(read_events(runtime._address(run).journal_path).events)
        print("\n== screen BEFORE the continuation\n" + screen(socket_path), flush=True)
        delivered = runtime.deliver(
            run, pointer, subject="worker-continuation", transport=Transport(before_send=activate)
        )
        time.sleep(3.0)
        print("\n== screen AFTER the continuation\n" + screen(socket_path), flush=True)
        evidence = getattr(delivered.delivery, "evidence", None) or delivered.evidence
        print(
            f"\ncontinuation: status={delivered.status} ok={delivered.ok} reason={delivered.reason!r} "
            f"turn_confirmed={getattr(evidence, 'turn_confirmed', None)} "
            f"submits={getattr(evidence, 'submit_count', None)} "
            f"stopped_now={head_process_status(run.pid_file).get('stopped')}",
            flush=True,
        )
        for event in read_events(runtime._address(run).journal_path).events[floor:]:
            if event.get("kind", "").startswith(("input.", "turn.")):
                keep = {k: event.get(k) for k in ("kind", "subject", "bytes", "reason") if event.get(k)}
                print(f"journal: {keep}", flush=True)
        if delivered.ok:
            await_quiet(socket_path)
        messages = transcript_user_messages(workspace)
        print(f"\ntranscript user messages: {json.dumps(messages, indent=1)}", flush=True)
        return 0 if delivered.ok else 1
    finally:
        if run is not None:
            try:
                signal_group(run.pid_file, signal.SIGCONT)
            except (KeyError, ValueError, OSError):
                pass
            runtime.stop(run, StopInitiator("operator", "secretary-1702 repro"))
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
