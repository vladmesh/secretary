"""Mechanical validation gate (secretary-633): the cheap-to-expensive layer 1 the pilot
dispatcher runs between worker-done and the LLM reviewer, and again right before a merge.

Ported from triggered_agents/agents/pipeline/validate.py (layer 1). A project declares where its
gate runs through its adapter's `validation.ci`:

  local  — run `validation.command` in the worker workspace; exit 0 is green, non-zero is red.
  github — publish the worker branch and poll GitHub CI for its head sha; SUCCESS is green,
           FAILURE is red, PENDING/NONE is pending (a check still running, or none posted yet —
           «CI не стартовал», deliberately not confused with «CI красный»).
  none   — no mechanical gate; the card goes straight to review (unchanged pre-633 behaviour).

Base-freshness recovery runs first for local/github: a branch that fell behind its base is
fast-forwarded by merging origin/<base> in, so the gate reflects the post-merge tree and a plain
`push branch:main` at merge time stays a fast-forward. A real textual conflict is a red verdict —
the card goes back to the worker to resolve it, never silently merged.

The gate is host I/O, so it lives behind CommandHostRuntime.gate_check; the runtime state machine
(dispatcher.py) turns a GateResult into a board move. Kept a pure function of the host so the
FakeHost in tests can stub gate_check without touching git/gh.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from secretary.dispatcher_helpers import _legacy_worker_branch, _tail
from secretary.dispatcher_types import HostError

# How long a github CI rollup may sit non-terminal (PENDING/NONE) before the pending watchdog
# escalates the card to Blocked — a required check nothing ever posts, a job waiting on manual
# environment approval, or a removed workflow would otherwise leave the card unwatched forever.
GATE_PENDING_STALL_SECONDS = int(os.environ.get("SECRETARY_GATE_PENDING_STALL_SECONDS", str(6 * 3600)))

_GH_TIMEOUT_S = 120
_FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
_RUN_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")


@dataclass
class GateResult:
    status: str  # "green" | "red" | "pending"
    summary: str
    log: str = ""


def gate_check(host, task: dict, record) -> GateResult:
    """Run the mechanical gate for `task` in the worker workspace. Raises HostError on gate infra
    failures (missing workspace, git/gh unreachable); returns a GateResult otherwise."""
    if getattr(host, "mode", "real") == "noop":
        return GateResult("green", "noop gate")
    ci = _validation(host, task).get("ci") or "none"
    if ci == "none":
        return GateResult("green", "ci none: mechanical gate skipped")
    workspace = record.workspace
    if not workspace or not Path(workspace).is_dir():
        raise HostError("gate workspace is missing")
    base = host.catalog.default_branch(task["project"], task.get("workspace", {}).get("base_branch"))
    if _recover_base(host, workspace, base) == "conflict":
        return GateResult(
            "red",
            f"branch fell behind base {base!r} and the merge conflicts — resolve it in the "
            f"workspace and report done again",
        )
    if ci == "local":
        return _local_gate(host, task, record, workspace)
    if ci == "github":
        return _github_gate(host, task, workspace, base)
    return GateResult("green", f"ci {ci!r}: no mechanical gate")


def _validation(host, task: dict) -> dict:
    adapter = host.catalog.adapter(task["project"])
    validation = adapter.get("validation") if isinstance(adapter, dict) else None
    return validation if isinstance(validation, dict) else {}


def _recover_base(host, workspace: str, base: str) -> str:
    """Fast-forward the worker branch onto the latest base. Returns "clean" (already current),
    "recovered" (merged base in), or "conflict" (a textual conflict, aborted)."""
    host._run(["git", "-C", workspace, "fetch", "origin", base], "gate base fetch")
    behind = host._run(
        ["git", "-C", workspace, "rev-list", "--count", f"HEAD..origin/{base}"],
        "gate base compare",
    ).stdout.strip()
    if behind in ("", "0"):
        return "clean"
    merge = host.run_capture(
        ["git", "-C", workspace, "merge", "--no-edit", f"origin/{base}"], "gate base merge"
    )
    if merge.returncode != 0:
        host.run_capture(["git", "-C", workspace, "merge", "--abort"], "gate base merge abort")
        return "conflict"
    return "recovered"


def _local_gate(host, task: dict, record, workspace: str) -> GateResult:
    command = _validation(host, task).get("command")
    if not isinstance(command, str) or not command.strip():
        raise HostError("local validation has no command")
    completed = host.run_capture(["bash", "-lc", command], "local gate", cwd=Path(workspace))
    if completed.returncode == 0:
        return GateResult("green", "local validation passed")
    tail = _tail((completed.stderr or completed.stdout or "").strip()) or "(no output)"
    return GateResult("red", "local validation failed", tail)


def _github_gate(host, task: dict, workspace: str, base: str) -> GateResult:
    branch = _legacy_worker_branch(task["ref"])
    host._run(["git", "-C", workspace, "push", "origin", f"{branch}:{branch}"], "gate publish branch")
    sha = host._run(["git", "-C", workspace, "rev-parse", "HEAD"], "gate head sha").stdout.strip()
    repo = _name_with_owner(host, workspace)
    rollup, failed = _poll_ci(host, repo, sha)
    short = sha[:12] or sha
    if rollup == "SUCCESS":
        return GateResult("green", f"CI green for `{branch}` @ `{short}`")
    if rollup == "FAILURE":
        job = (failed or {}).get("name") or (failed or {}).get("context") or "?"
        log = _failed_log(host, repo, failed or {})
        return GateResult("red", f"CI red: job «{job}» failed on `{branch}` @ `{short}`", log or "")
    return GateResult("pending", f"CI {rollup.lower()} for `{branch}` @ `{short}` — no terminal result yet")


def _name_with_owner(host, workspace: str) -> str:
    completed = host.run_capture(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        "gate repo view",
        cwd=Path(workspace),
    )
    name = (completed.stdout or "").strip()
    if completed.returncode != 0 or not name:
        raise HostError("gate could not resolve the repository name")
    return name


def _poll_ci(host, repo: str, sha: str) -> tuple[str, dict | None]:
    """Combined CI rollup for `sha`: GitHub-Actions check-runs plus legacy commit statuses."""
    items: list[dict] = []
    runs = _gh_api(host, f"repos/{repo}/commits/{sha}/check-runs", jq=".check_runs")
    if isinstance(runs, list):
        items.extend(item for item in runs if isinstance(item, dict))
    statuses = _gh_api(host, f"repos/{repo}/commits/{sha}/status", jq=".statuses")
    if isinstance(statuses, list):
        items.extend(item for item in statuses if isinstance(item, dict))
    return _rollup(items)


def _gh_api(host, path: str, *, jq: str):
    completed = host.run_capture(["gh", "api", path, "--jq", jq], "gate gh api")
    if completed.returncode != 0:
        raise HostError(f"gate gh api failed: {_tail((completed.stderr or completed.stdout or '').strip())}")
    text = (completed.stdout or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except ValueError:
        return []


def _check_result(item: dict) -> str:
    """One rollup entry -> 'pass' | 'fail' | 'pending'. Handles both a GitHub-Actions check-run
    (status/conclusion) and a legacy commit status (state)."""
    if "state" in item and "status" not in item:
        state = str(item.get("state", "")).upper()
        if state in ("FAILURE", "ERROR"):
            return "fail"
        if state == "SUCCESS":
            return "pass"
        return "pending"
    if str(item.get("status", "")).upper() != "COMPLETED":
        return "pending"
    return "fail" if str(item.get("conclusion", "")).upper() in _FAIL_CONCLUSIONS else "pass"


def _rollup(items: list[dict]) -> tuple[str, dict | None]:
    """Overall CI state: PENDING while any job is still running (even next to an already-failed
    one — a flaky-looking early failure must not bounce the card before the suite finishes), then
    FAILURE (with the first failing entry) once every job is terminal, SUCCESS if none failed, or
    NONE when there are no checks at all."""
    if not items:
        return "NONE", None
    first_fail = None
    pending = False
    for item in items:
        result = _check_result(item)
        if result == "fail" and first_fail is None:
            first_fail = item
        elif result == "pending":
            pending = True
    if pending:
        return "PENDING", None
    if first_fail is not None:
        return "FAILURE", first_fail
    return "SUCCESS", None


def _failed_log(host, repo: str, item: dict, lines: int = 40) -> str | None:
    """Tail of the failed job's log via `gh run view --log-failed`, or None when the entry is not
    an Actions run (e.g. an external status context) or gh cannot fetch it."""
    match = _RUN_URL_RE.search(str(item.get("details_url") or item.get("html_url") or item.get("targetUrl") or ""))
    if not match:
        return None
    run_id = match.group(2)
    completed = host.run_capture(
        ["gh", "run", "view", run_id, "-R", repo, "--log-failed"], "gate failed log"
    )
    if completed.returncode != 0:
        return None
    tail = "\n".join((completed.stdout or "").strip().splitlines()[-lines:])
    return tail or None
