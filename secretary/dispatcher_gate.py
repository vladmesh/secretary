"""Mechanical validation gate (secretary-633): the cheap-to-expensive layer 1 the dispatcher runs
between worker-done and the LLM reviewer, and again right before a merge.

A project declares where its gate runs through its adapter's `validation.ci`:

  local  — run `validation.command` in the worker workspace; exit 0 is green, non-zero is red.
  github — publish the worker branch, ensure an open PR into the project's base branch (so the
           typical `on: [push:main, pull_request]` workflow actually fires — a bare feature-branch
           push triggers nothing), then poll GitHub CI for the branch head sha; SUCCESS is green,
           FAILURE is red, PENDING/NONE is pending (a check still running, or none posted yet —
           "CI did not start", deliberately not confused with "CI is red"). `validation
           .required_checks` narrows the rollup to those check names; without it every check on
           the sha counts.
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

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path

from secretary.dispatcher_helpers import _legacy_worker_branch, _tail
from secretary.dispatcher_types import HostError

# How long a github CI rollup may sit non-terminal (PENDING/NONE) before the pending watchdog
# escalates the card to Blocked — a required check nothing ever posts, a job waiting on manual
# environment approval, or a removed workflow would otherwise leave the card unwatched forever.
GATE_PENDING_STALL_SECONDS = int(os.environ.get("SECRETARY_GATE_PENDING_STALL_SECONDS", str(6 * 3600)))

# Single knob for how much of a failed gate's log reaches the worker (secretary-766): both the
# local-gate stderr/stdout tail and the github-gate `--log-failed` fragment size read this, so
# there is one place to widen or narrow the excerpt instead of a magic number per call site.
GATE_LOG_FRAGMENT_LINES = int(os.environ.get("SECRETARY_GATE_LOG_FRAGMENT_LINES", "40"))

_FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
_RUN_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")
# `gh run view --log[-failed]` emits one line per log entry as `<job>\t<step>\t<content>`.
_ERROR_MARK_RE = re.compile(r"##\[error\]")
# The runner's own generic completion echo — posted with the same `##[error]` marker as a real
# cause, by both a failing step and a `needs: [...]` aggregator's summary script. A filter that
# just keeps `##[error]` lines happily lands on this instead of the actual error above it
# (secretary-766); it is never the cause, only noise the runner always appends.
_RUNNER_BOILERPLATE_RE = re.compile(r"(?i)process completed with exit code \d+")
# Content-level signs of a preparation/infra failure rather than a test/assertion failure — the
# gate can't run the code under test if the registry, network, or dependency install is down, and
# a worker that reads that as "my code is broken" edits the wrong thing (secretary-766).
_INFRA_MARK_RE = re.compile(
    r"(?i)\b(docker|registry|pull access denied|no space left|econnrefused|"
    r"could not resolve host|network is unreachable|connection (?:reset|refused|timed out)|"
    r"temporary failure in name resolution|failed to fetch|apt-get|npm err! network|"
    r"pip.{0,20}(?:could not find|could not install)|dependency resolution)\b"
)


@dataclass
class GateResult:
    status: str  # "green" | "red" | "pending"
    summary: str
    log: str = ""
    # Stable identity of the failure, independent of the head SHA (which changes on every rework
    # commit): what the repeat-bounce check compares round to round. Empty for green/pending.
    fingerprint: str = ""
    # A green result is reusable evidence only when it names the exact tree and the terminal
    # checks which judged it.  The dispatcher persists this plain JSON object with the card so it
    # can hand the same receipt to review, Assessment and the release audit without asking another
    # role to repeat a broad suite.
    attestation: dict[str, object] | None = None


@dataclass
class _LogFragment:
    """What `_failed_log` recovered from a failed CI job, or why it couldn't."""

    available: bool
    job: str = ""
    step: str = ""
    text: str = ""
    infra: bool = False
    reason: str = ""


def _fingerprint(*parts: str) -> str:
    """Short stable digest of `parts` for repeat-bounce comparison. A GitHub `detail` always
    carries the head SHA (secretary-766); hashing job/step/error text instead of the rendered
    summary keeps the same underlying failure recognisable across rework commits."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def gate_check(host, task: dict, record) -> GateResult:
    """Run the mechanical gate for `task` in the worker workspace. Raises HostError on gate infra
    failures (missing workspace, git/gh unreachable); returns a GateResult otherwise."""
    if getattr(host, "mode", "real") == "noop":
        return GateResult("green", "noop gate", attestation=_attestation(
            validated_sha="", base_sha="", gate_mode="noop", required_checks=[], source="noop"
        ))
    ci = _validation(host, task).get("ci") or "none"
    workspace = record.workspace
    if ci != "none" and (not workspace or not Path(workspace).is_dir()):
        raise HostError("gate workspace is missing")
    base = host.catalog.default_branch(task["project"], task.get("workspace", {}).get("base_branch"))
    if ci == "none":
        return GateResult("green", "ci none: mechanical gate skipped", attestation=_attestation(
            validated_sha=_head_sha(host, workspace), base_sha=_base_sha(host, workspace, base),
            gate_mode="none", required_checks=[], source="none",
        ))
    if _recover_base(host, workspace, base) == "conflict":
        return GateResult(
            "red",
            f"branch fell behind base {base!r} and the merge conflicts — resolve it in the "
            f"workspace and report done again",
            fingerprint=_fingerprint("base-conflict", base),
            attestation=_attestation(
                validated_sha=_head_sha(host, workspace), base_sha=_base_sha(host, workspace, base),
                gate_mode=ci, required_checks=[], source="base-conflict",
            ),
        )
    if ci == "local":
        return _local_gate(host, task, record, workspace)
    if ci == "github":
        return _github_gate(host, task, workspace, base, _required_checks(host, task))
    return GateResult("green", f"ci {ci!r}: no mechanical gate", attestation=_attestation(
        validated_sha=_head_sha(host, workspace), base_sha=_base_sha(host, workspace, base),
        gate_mode=ci, required_checks=[], source=ci,
    ))


def _validation(host, task: dict) -> dict:
    adapter_fn = getattr(host.catalog, "adapter", None)
    adapter = adapter_fn(task["project"]) if callable(adapter_fn) else None
    validation = adapter.get("validation") if isinstance(adapter, dict) else None
    return validation if isinstance(validation, dict) else {}


def _required_checks(host, task: dict) -> list[str]:
    """Declared names of the checks the github gate judges by (`validation.required_checks`).
    Empty list means the adapter has not migrated yet: the gate then judges by every check on the
    sha, as it did before secretary-841."""
    declared = _validation(host, task).get("required_checks")
    if not isinstance(declared, list):
        return []
    return [name.strip() for name in declared if isinstance(name, str) and name.strip()]


def validation_ci(host, task: dict) -> str:
    """The project's declared mechanical-gate mode: "local" | "github" | "none". Read by the
    dispatcher's merge step to pick a github PR merge over the local fast-forward."""
    return _validation(host, task).get("ci") or "none"


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
    receipt = _attestation(
        validated_sha=_head_sha(host, workspace),
        base_sha=_base_sha(host, workspace, host.catalog.default_branch(task["project"], task.get("workspace", {}).get("base_branch"))),
        gate_mode="local",
        required_checks=[{"name": "local validation", "conclusion": "SUCCESS" if completed.returncode == 0 else "FAILURE", "url": ""}],
        source=command,
    )
    if completed.returncode == 0:
        return GateResult("green", "local validation passed", attestation=receipt)
    tail = _tail((completed.stderr or completed.stdout or "").strip(), GATE_LOG_FRAGMENT_LINES) or "(no output)"
    summary = "local validation failed"
    if _INFRA_MARK_RE.search(tail):
        summary += "; this looks like an infrastructure setup failure rather than a test failure"
    return GateResult("red", summary, tail, fingerprint=_fingerprint("local", tail), attestation=receipt)


def _github_gate(host, task: dict, workspace: str, base: str, required: list[str] | None = None) -> GateResult:
    branch = _legacy_worker_branch(task["ref"])
    host._run(["git", "-C", workspace, "push", "origin", f"{branch}:{branch}"], "gate publish branch")
    _ensure_pr(host, workspace, task, branch, base)
    sha = host._run(["git", "-C", workspace, "rev-parse", "HEAD"], "gate head sha").stdout.strip()
    repo = _name_with_owner(host, workspace)
    rollup, failed, checked = _poll_ci(host, repo, sha, required or [])
    receipt = _attestation(
        validated_sha=sha,
        base_sha=_base_sha(host, workspace, base),
        gate_mode="github",
        required_checks=[_terminal_check(item) for item in checked],
        source=json.dumps([_terminal_check(item) for item in checked], sort_keys=True, separators=(",", ":")),
    )
    short = sha[:12] or sha
    if rollup == "SUCCESS":
        return GateResult("green", f"CI green for `{branch}` @ `{short}`", attestation=receipt)
    if rollup == "FAILURE":
        job = (failed or {}).get("name") or (failed or {}).get("context") or "?"
        fragment = _failed_log(host, repo, failed or {})
        where = f"job «{job}»"
        if fragment.step:
            where += f", step \"{fragment.step}\""
        summary = f"CI red: {where} failed on `{branch}` @ `{short}`"
        if fragment.infra:
            summary += "; this looks like an infrastructure setup failure rather than a test failure"
        log = fragment.text if fragment.available else f"log unavailable: {fragment.reason}"
        cause = fragment.text if fragment.available else f"unavailable:{fragment.reason}"
        fingerprint = _fingerprint("github", job, fragment.step, cause)
        return GateResult("red", summary, log, fingerprint=fingerprint, attestation=receipt)
    return GateResult(
        "pending", f"CI {rollup.lower()} for `{branch}` @ `{short}` — no terminal result yet",
        attestation=receipt,
    )


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


def _ensure_pr(host, workspace: str, task: dict, branch: str, base: str) -> None:
    """Ensure an open PR from the worker branch into `base` exists so the project's
    `pull_request` CI runs (a bare feature-branch push fires nothing on the typical
    `on: [push:main, pull_request]` workflow). Idempotent: an already-open PR is reused, and a
    concurrent tick or gh refusing to duplicate a PR is tolerated as long as one is open."""
    if _open_pr_number(host, workspace, branch) is not None:
        return
    title = f"{task['ref']}: {branch}"
    body = (
        f"Automatic PR for worker branch `{branch}` of task {task['ref']}. "
        f"Opened by the CI gate so that the pull_request CI runs."
    )
    created = host.run_capture(
        ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body],
        "gate pr create",
        cwd=Path(workspace),
    )
    if created.returncode == 0 or _open_pr_number(host, workspace, branch) is not None:
        return
    text = (created.stderr or created.stdout or "").strip()
    raise HostError(f"gate could not open a PR for {branch!r}: {_tail(text)}")


def _open_pr_number(host, workspace: str, branch: str) -> int | None:
    """Number of the open PR whose head is `branch`, or None when none is open. `gh pr list`
    exits 0 with empty output when nothing matches, so no-PR is not confused with a gh failure."""
    completed = host.run_capture(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number", "-q", ".[0].number"],
        "gate pr list",
        cwd=Path(workspace),
    )
    if completed.returncode != 0:
        return None
    text = (completed.stdout or "").strip()
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _poll_ci(host, repo: str, sha: str, required: list[str] | None = None) -> tuple[str, dict | None, list[dict]]:
    """Combined CI rollup for `sha`: GitHub-Actions check-runs plus legacy commit statuses,
    narrowed to `required` when the adapter declares a required set."""
    items: list[dict] = []
    runs = _gh_api(host, f"repos/{repo}/commits/{sha}/check-runs", jq=".check_runs")
    if isinstance(runs, list):
        items.extend(item for item in runs if isinstance(item, dict))
    statuses = _gh_api(host, f"repos/{repo}/commits/{sha}/status", jq=".statuses")
    if isinstance(statuses, list):
        items.extend(item for item in statuses if isinstance(item, dict))
    rollup, failed = _rollup(items, required)
    return rollup, failed, _selected_checks(items, required or [])


def _selected_checks(items: list[dict], required: list[str]) -> list[dict]:
    """The exact checks the gate judged, in stable order, for an attestation receipt."""
    selected = [item for item in items if not required or _check_name(item) in required]
    return sorted(selected, key=lambda item: (_check_name(item), str(item.get("id") or item.get("context") or "")))


def _terminal_check(item: dict) -> dict[str, str]:
    conclusion = str(item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
    return {
        "name": _check_name(item),
        "conclusion": conclusion,
        "url": str(item.get("details_url") or item.get("html_url") or item.get("target_url") or item.get("targetUrl") or ""),
    }


def _head_sha(host, workspace: str) -> str:
    if not workspace:
        return ""
    try:
        return host._run(["git", "-C", workspace, "rev-parse", "HEAD"], "gate attestation head").stdout.strip()
    except HostError:
        return ""


def _base_sha(host, workspace: str, base: str) -> str:
    if not workspace or not base:
        return ""
    try:
        return host._run(["git", "-C", workspace, "rev-parse", f"origin/{base}"], "gate attestation base").stdout.strip()
    except HostError:
        return ""


def _attestation(*, validated_sha: str, base_sha: str, gate_mode: str, required_checks: list[dict[str, str]], source: str) -> dict[str, object]:
    """Canonical, redaction-safe receipt for one mechanical gate result.

    The digest intentionally carries only the command/workflow identity, never command output.
    Check URLs and conclusions remain inspectable evidence, while any output or secret-like value
    stays in the existing scrubbed failure path.
    """
    digest = hashlib.sha256(source.encode("utf-8", "surrogateescape")).hexdigest()
    return {
        "validated_sha": validated_sha,
        "base_sha": base_sha,
        "gate_mode": gate_mode,
        "required_checks": required_checks,
        "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "command_or_workflow_digest": digest,
    }


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


def _check_name(item: dict) -> str:
    """Declared-name key of a rollup entry: the Actions check-run `name`, or the legacy commit
    status `context`."""
    return str(item.get("name") or item.get("context") or "").strip()


def _rollup(items: list[dict], required: list[str] | None = None) -> tuple[str, dict | None]:
    """Overall CI state: PENDING while any job is still running (even next to an already-failed
    one — a flaky-looking early failure must not bounce the card before the suite finishes), then
    FAILURE (with the first failing entry) once every job is terminal, SUCCESS if none failed, or
    NONE when there are no checks at all.

    With a declared `required` set the gate judges by those names only (secretary-841): entries
    with any other name are ignored whatever they report, and a required name with no entry on the
    sha at all counts as pending — the check may still be queued, and a set nothing ever posts is
    what the pending watchdog escalates. Without a set every entry counts, the pre-841 behaviour
    an adapter that has not declared `validation.required_checks` keeps.
    """
    if required:
        by_name = {name: [] for name in required}
        for item in items:
            bucket = by_name.get(_check_name(item))
            if bucket is not None:
                bucket.append(item)
        items = [item for name in required for item in by_name[name]]
        if any(not bucket for bucket in by_name.values()):
            # a required check has not been posted for this sha yet
            return "PENDING", None
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


def _parse_job_log(text: str) -> list[tuple[str, str, str]]:
    """Split `gh run view --log[-failed]` output into (job, step, content) triples. A line that
    doesn't carry the tab-separated job/step prefix (unexpected gh output) still surfaces with
    empty job/step rather than being dropped."""
    entries = []
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            entries.append((parts[0], parts[1], parts[2]))
        else:
            entries.append(("", "", line))
    return entries


def _failed_log(host, repo: str, item: dict, lines: int = GATE_LOG_FRAGMENT_LINES) -> _LogFragment:
    """Job, step, and an error-focused fragment of the failed job's log via
    `gh run view --log-failed`.

    `--log-failed` interleaves every failed job in the run, including any job that only
    aggregates other jobs' results (`needs: [...]`, a bash script that echoes a summary and
    exits non-zero); a blind tail of the whole dump often lands on that echo instead of the
    actual error further up. Scoping to the job GitHub's rollup reported as failed removes the
    aggregator's own lines. Within that job, the fragment is a window ending at the last
    `##[error]` line that isn't just the runner's generic completion echo (that echo carries the
    same marker as a real cause, and a filter that keeps any `##[error]` line happily lands on it
    instead — secretary-766); a job whose real error was never marked `##[error]` at all (plain
    stdout, e.g. a bare Python traceback) falls back to the job's own tail, same as before.
    """
    match = _RUN_URL_RE.search(str(item.get("details_url") or item.get("html_url") or item.get("targetUrl") or ""))
    if not match:
        return _LogFragment(available=False,
                            reason="the entry is not an Actions run (no run link)")
    run_id = match.group(2)
    completed = host.run_capture(
        ["gh", "run", "view", run_id, "-R", repo, "--log-failed"], "gate failed log"
    )
    if completed.returncode != 0:
        return _LogFragment(available=False, reason="`gh run view --log-failed` returned an error")
    entries = _parse_job_log((completed.stdout or "").strip())
    if not entries:
        return _LogFragment(available=False, reason="the gate received an empty log")
    job_name = str(item.get("name") or item.get("context") or "")
    scoped = [entry for entry in entries if not job_name or entry[0] == job_name] or entries
    cause_idx = next(
        (
            i
            for i in range(len(scoped) - 1, -1, -1)
            if _ERROR_MARK_RE.search(scoped[i][2]) and not _RUNNER_BOILERPLATE_RE.search(scoped[i][2])
        ),
        len(scoped) - 1,
    )
    start = max(0, cause_idx - lines + 1)
    tail = scoped[start : cause_idx + 1]
    step = next((entry[1] for entry in reversed(tail) if entry[1]), "")
    text = "\n".join(entry[2] for entry in tail).strip()
    if not text:
        return _LogFragment(available=False, reason="the gate received an empty log")
    return _LogFragment(available=True, job=job_name, step=step, text=text, infra=bool(_INFRA_MARK_RE.search(text)))
