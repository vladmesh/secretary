"""Fake `claude` and `codex` executables for PO head turns, shared by the runner and `/po` tests.

Each fake logs its argv, cwd and stdin to `$FAKE_LOG`, prints an event stream that carries reasoning
and a tool call beside the final answer, and changes behaviour on words in the owner's message:
`SLEEP` keeps the turn running with a child in its process group, `FAIL` exits non-zero, `SILENT`
exits zero without a final answer, `NOPERSIST` makes Claude save no conversation.
"""

from __future__ import annotations

import time

SETTLE_SECONDS = 30

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
prompt = sys.stdin.read()
argv = sys.argv[1:]
log = os.environ["FAKE_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps({"cli": "claude", "argv": argv, "cwd": os.getcwd(), "prompt": prompt}) + "\n")
flag = "--resume" if "--resume" in argv else "--session-id"
session = argv[argv.index(flag) + 1]
# Claude 2.1.270's own refusals: a saved conversation cannot be created again, a missing one resumed.
saved_path = log + ".saved"
saved = set(open(saved_path).read().split()) if os.path.exists(saved_path) else set()
if flag == "--resume" and session not in saved:
    print(f"No conversation found with session ID: {session}", file=sys.stderr)
    sys.exit(1)
if flag == "--session-id" and session in saved:
    print(f"Error: Session ID {session} is already in use.", file=sys.stderr)
    sys.exit(1)
if "NOPERSIST" not in prompt:
    with open(saved_path, "a") as handle:
        handle.write(session + "\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "thinking", "thinking": "THINKING-SECRET"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "TOOL-CALL-SECRET"}}]}}), flush=True)
if "SLEEP" in prompt:
    child = subprocess.Popen(["sleep", "300"])
    with open(log + ".pids", "a") as handle:
        handle.write(f"{os.getpid()} {child.pid}\n")
    time.sleep(300)
if "FAIL" in prompt:
    print("boom from fake claude", file=sys.stderr)
    sys.exit(3)
if "SILENT" in prompt:
    sys.exit(0)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": session, "result": f"claude {flag} {session}: {prompt}"}))
"""

FAKE_CODEX = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
prompt = sys.stdin.read()
argv = sys.argv[1:]
log = os.environ["FAKE_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps({"cli": "codex", "argv": argv, "cwd": os.getcwd(), "prompt": prompt}) + "\n")
resume = argv[:2] == ["exec", "resume"]
thread = argv[-2] if resume else "019a-fake-thread"
out = argv[argv.index("-o") + 1]
print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "THINKING-SECRET"}}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "TOOL-CALL-SECRET"}}), flush=True)
if "SLEEP" in prompt:
    child = subprocess.Popen(["sleep", "300"])
    with open(log + ".pids", "a") as handle:
        handle.write(f"{os.getpid()} {child.pid}\n")
    time.sleep(300)
if "FAIL" in prompt:
    print("boom from fake codex", file=sys.stderr)
    sys.exit(4)
if "SILENT" in prompt:
    sys.exit(0)
with open(out, "w") as handle:
    handle.write(f"codex {'resume' if resume else 'new'} {thread}: {prompt}\n")
print(json.dumps({"type": "turn.completed"}))
"""


def eventually(predicate, message: str, timeout: float = SETTLE_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)
