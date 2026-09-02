"""Mechanical validation gate: the cheap-to-expensive layer 1 the dispatcher runs between
worker-done and the LLM reviewer, and again right before a merge.

A project declares where its gate runs through its adapter's `validation.ci`:

  local  — run `validation.command` in the worker workspace; exit 0 is green, non-zero is red.
  github — publish the worker branch, ensure an open PR into the project's base branch, then
           poll GitHub CI for the branch head sha; SUCCESS is green, FAILURE is red,
           PENDING/NONE is pending (deliberately not confused with "CI is red").
           `validation.required_checks` narrows the rollup to those check names.
  none   — no mechanical gate; the card goes straight to review.

Candidate history is checked in every mode, `none` included, before anything is published: a
forbidden AI co-author trailer is a red gate with a local repair, never a push followed by a
history rewrite. Base-freshness recovery runs first for local/github, so the gate reflects the
post-merge tree; a real textual conflict is a red verdict, never a silent merge.

The gate is host I/O, so it lives behind CommandHostRuntime.gate_check and stays a pure
function of the host; dispatcher.py turns a GateResult into a board move.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from secretary.candidate_history import (
    Commit,
    ai_attributions,
    parse_shas,
    repair_message,
)
from secretary.dispatcher_gate_receipt import is_exact_sha, mint_gate_receipt
from secretary.dispatcher_helpers import (
    _last_marker_body,
    _legacy_worker_branch,
    _tail,
    safe_one_line,
    scrub_host_output,
)
from secretary.dispatcher_types import GateTransportError, HostError

# Pending CI has a bounded watchdog so missing checks cannot strand a card.
GATE_PENDING_STALL_SECONDS = int(os.environ.get("SECRETARY_GATE_PENDING_STALL_SECONDS", str(6 * 3600)))

GATE_LOG_FRAGMENT_LINES = int(os.environ.get("SECRETARY_GATE_LOG_FRAGMENT_LINES", "40"))

# Consecutive backend silence is bounded before human escalation.
GATE_TRANSPORT_MAX_ATTEMPTS = max(1, int(os.environ.get("SECRETARY_GATE_TRANSPORT_MAX_ATTEMPTS", "5")))

# Enumerated CI-service failures rerun per SHA with a bounded cap.
GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS = max(
    1, int(os.environ.get("SECRETARY_GATE_INFRASTRUCTURE_RERUN_MAX_ATTEMPTS", "2"))
)

PR_BODY_SECTION_CHARS = int(os.environ.get("SECRETARY_PR_BODY_SECTION_CHARS", "4000"))

_PR_BODY_FOOTER = (
    "Opened by the secretary CI gate so that the `pull_request` workflow runs. "
    "Title and body are built from the card and the worker's done report."
)

_FAIL_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STALE", "STARTUP_FAILURE"}
_RUN_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")
_NO_DIFF_WORKFLOW = "ci.yml"
_WORKFLOW_RUN_FIELDS = "databaseId,headSha,status,conclusion,url"
# `gh run view --log[-failed]` emits one line per log entry as `<job>\t<step>\t<content>`.
_ERROR_MARK_RE = re.compile(r"##\[error\]")
_RUNNER_BOILERPLATE_RE = re.compile(r"(?i)process completed with exit code \d+")
# CI-service signatures distinguish infrastructure from code/test failures.
# Only explicit CI service unavailable/5xx signatures classify infrastructure reds.
_ACTION_DOWNLOAD_5XX_RE = re.compile(
    # The runner prints its preparation notice and the failed download on separate entries.  The
    # fragment, not a source line, is the unit of adjacency because `_failed_log` joins entries
    # with newlines.  Requiring the action-download wording on the 5xx entry keeps an unrelated
    # setup failure from borrowing the preparation notice above it.
    r"(?im)\bgetting action download info\b(?:[^\n]*\n){0,3}[^\n]*"
    r"(?:failed to download action|response status code does not indicate success)[^\n]*\b5\d\d\b"
)
_REGISTRY_UNAVAILABLE_RE = re.compile(
    # A named registry plus a service-unavailable response is explicit evidence.  Docker/Buildx
    # daemon messages are explicit too even when they omit the registry name: they name an HTTP
    # boundary and a 5xx themselves.  The caller still binds this to one of the container steps.
    r"(?im)(?:"
    r"(?:registry(?:[./][\w./:-]+)?|ghcr\.io|docker\.io|quay\.io|image manifest|pulling fs layer)"
    r"[^\n]{0,240}?(?:http(?:/\d(?:\.\d)?)?\s*(?:status\s*)?[: ]?5\d\d|"
    r"(?:service|temporarily) unavailable)"
    r"|(?:received unexpected http status|unexpected status(?: code)?)\s*:?\s*5\d\d"
    r")"
)
_RUNNER_UNAVAILABLE_RE = re.compile(
    # These are runner-service diagnostics, not arbitrary setup-script prose that happens to use
    # the word "runner".  Keep the vocabulary enumerable with the fixture corpus below.
    r"(?i)(?:the hosted runner failed to start|the runner has (?:received a shutdown signal|"
    r"lost communication with the server)|the job was cancelled because the runner was not started)"
)
# How a failed backend command says that an answer *did* arrive.
#
# The rule is positive and the default is silence: a question counts as answered only when the
# tool prints one of the shapes below, and anything else it prints is a question that got no
# answer. Guessing "no answer" costs a few retries; guessing "answer" costs an immediate wrong
# Blocked on a moment of bad network. The shapes were taken from the binaries this gate runs
# (gh 2.45.0, git 2.43.0); the captures are in the tests next to them.
#
# 1. An HTTP status the tool quotes — only the backend can produce one. A 5xx is the exception:
#    the backend failed to serve an answer, and that counts as transport.
#      gh api        -> gh: Not Found (HTTP 404)
#      git over HTTPS-> fatal: unable to access '...': The requested URL returned error: 503
_HTTP_STATUS_RE = re.compile(r"(?i)\bhttp(?:/\d(?:\.\d)?)?[ /](\d{3})\b|returned error:\s*(\d{3})\b")
# 2. A GraphQL error: gh rendering an API response body it parsed. A raw response body reaching
#    the text counts for the same reason.
#      gh repo view  -> GraphQL: Could not resolve to a Repository with the name '...'
# 3. git's push report, which exists only because the remote answered: the per-ref status table
#    it prints from the server's report-status, and any line the server itself sent.
#      git push      ->  ! [rejected]        main -> main (fetch first)
#                        remote: policy: branch is protected
_ANSWERED_RE = re.compile(
    r"(?im)(\bgraphql:\s|^\s*remote:\s|!\s*\[(?:rejected|remote rejected|deleted|no match)\]"
    r'|^\s*\{\s*"(?:errors|message)")'
)
# 4. `git fetch` reporting that the requested remote ref does not exist. This is a terminal
# answer from the named remote, but git gives it neither an HTTP status nor the push report shape
# above. Keep it coupled to the fetch invocation: `fatal` by itself remains unclassified silence.
_GIT_FETCH_MISSING_REF_RE = re.compile(r"(?im)^fatal: couldn't find remote ref [^\r\n]+$")


def _backend_answered(text: str, args: list[str]) -> bool:
    """Did the gate's backend answer this failed backend command?

    An HTTP status decides first (5xx is the backend failing to answer, not an answer); otherwise
    one of the answer shapes above must be present. A missing ref is also an answer, but only from
    `git fetch`. Text matching none of them is a question that got no answer.
    """
    text = (text or "").strip()
    if not text:
        return False
    status = _HTTP_STATUS_RE.search(text)
    if status:
        code = status.group(1) or status.group(2)
        return not code.startswith("5")
    if args[:1] == ["git"] and "fetch" in args and _GIT_FETCH_MISSING_REF_RE.search(text):
        return True
    return bool(_ANSWERED_RE.search(text))


def _backend_call(host, args: list[str], label: str, *, cwd: Path | None = None):
    """Ask the gate's backend, and be the one place that decides no answer came back.

    Every question this gate puts to a remote goes through here and nothing else does, which is
    what keeps a determinate local failure out of the transport class however its message reads.
    Returns the CompletedProcess, non-zero ones that carry an answer included; raises
    GateTransportError when the tool could not run or its output says it never got through. The
    tool's own text always travels with the failure — the classification has nothing else to read.
    """
    try:
        completed = host.run_capture(args, label, cwd=cwd)
    except HostError as exc:
        # The command never finished: it timed out waiting for the backend, or could not be run
        # at all. Either way no answer exists, and the tool's own text travels with it.
        raise GateTransportError(f"{label} got no answer: {exc}") from None
    if completed.returncode == 0:
        return completed
    text = _tail((completed.stderr or completed.stdout or "").strip())
    if _backend_answered(text, args):
        return completed
    raise GateTransportError(f"{label} got no answer: {text or '(no output)'}")


@dataclass
class GateResult:
    status: str  # "green" | "red" | "pending"
    summary: str
    log: str = ""
    fingerprint: str = ""
    # Structured terminal reds decide whether the exact SHA may rerun.
    failure_class: str = "substantive"  # "substantive" | "infrastructure"
    failure_reason: str = ""
    # The Actions run which supplied an infrastructure-classified red.  Only this concrete run
    # can be rerun; an external status or a log without a run URL remains a red verdict.
    failed_run_id: str = ""
    failed_run_repo: str = ""
    # Reusable green evidence names the exact tree and terminal checks.
    attestation: dict[str, object] | None = None


@dataclass
class _LogFragment:
    """What `_failed_log` recovered from a failed CI job, or why it couldn't."""

    available: bool
    step: str = ""
    text: str = ""
    failure_class: str = "substantive"
    failure_reason: str = ""
    reason: str = ""


def _fingerprint(*parts: str) -> str:
    """Short stable digest of `parts` for repeat-bounce comparison. A GitHub `detail` always
    carries the head SHA (secretary-766); hashing job/step/error text instead of the rendered
    summary keeps the same underlying failure recognisable across rework commits."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def gate_check(host, task: dict, record) -> GateResult:
    """Run the mechanical gate for `task` in the worker workspace.

    Raises `GateTransportError` when a question put to the backend got no answer, plain `HostError`
    for any determinate gate failure, and returns a GateResult otherwise, red answers included.
    """
    if getattr(host, "mode", "real") == "noop":
        # A noop proves no command ran.  It may preserve dispatcher control flow, but must never
        # look like reusable validation evidence.
        return GateResult("green", "noop gate")
    ci = _validation(host, task).get("ci") or "none"
    workspace = record.workspace
    # Every mode, including none, needs a readable checkout for candidate preflight.
    if not workspace or not Path(workspace).is_dir():
        raise HostError("gate workspace is missing")
    base = host.catalog.default_branch(task["project"], task.get("workspace", {}).get("base_branch"))
    if ci != "none" and _recover_base(host, workspace, base) == "conflict":
        return GateResult(
            "red",
            f"branch fell behind base {base!r} and the merge conflicts — resolve it in the "
            f"workspace and report done again",
            fingerprint=_fingerprint("base-conflict", base),
        )
    # Candidate trailer preflight precedes every gate mode and publication.
    history = _candidate_history_gate(host, workspace, base)
    if history is not None:
        return history
    if ci == "none":
        # `none` is an explicit absence of mechanical validation, not an all-green check set.
        return GateResult("green", "ci none: mechanical gate skipped")
    if ci == "local":
        return _local_gate(host, task, record, workspace)
    if ci == "github":
        return _github_gate(host, task, record, workspace, base, _required_checks(host, task))
    raise HostError(f"unsupported validation ci mode {ci!r}; expected local, github or none")


def _candidate_history_gate(host, workspace: str, base: str) -> GateResult | None:
    """Reject forbidden AI attribution in `base..HEAD` before anything is published.

    Runs ahead of the branch push, the local suite and the `none` exit, so a violation costs a
    rework round rather than a rewrite of published history. Only commit messages are read.

    The messages are read one commit at a time, keyed on object ids listed separately: framing
    several untrusted messages into one stream is forgeable, since a message may contain any byte
    including the delimiter. Nothing about a range that will not answer is a pass — an unreadable
    listing or message raises `HostError` and the card is blocked.
    """
    try:
        listing = host.run_capture(
            ["git", "-C", workspace, "log", "--format=%H", _history_range(host, workspace, base)],
            "gate candidate history",
        )
    except UnicodeError as exc:
        raise HostError("gate candidate history could not be decoded") from exc
    if listing.returncode != 0:
        raise HostError(
            "gate candidate history could not be read: "
            f"{_tail((listing.stderr or listing.stdout or '').strip())}"
        )
    try:
        shas = parse_shas(listing.stdout or "")
    except ValueError as exc:
        raise HostError(f"gate candidate history could not be read: {exc}") from None
    commits = []
    for sha in shas:
        try:
            message = host.run_capture(
                ["git", "-C", workspace, "log", "-1", "--format=%B", sha],
                "gate candidate history message",
            )
        except UnicodeError as exc:
            raise HostError(f"gate candidate history could not be decoded for {sha[:12]}") from exc
        if message.returncode != 0:
            raise HostError(
                f"gate candidate history could not be read for {sha[:12]}: "
                f"{_tail((message.stderr or message.stdout or '').strip())}"
            )
        commits.append(Commit(sha=sha, message=message.stdout or ""))
    attributions = ai_attributions(commits)
    if not attributions:
        return None
    return GateResult(
        "red",
        "candidate history carries forbidden AI attribution; nothing was published",
        repair_message(attributions, base),
        fingerprint=_fingerprint("ai-attribution", *(item.sha for item in attributions)),
    )


def _history_range(host, workspace: str, base: str) -> str:
    """`<base>..HEAD` for this checkout, named by a base ref that actually resolves here.

    `origin/<base>` where the gate fetched it, the local ref for a `none` project that never
    fetches, and a raise when neither resolves: a range silently widened to the whole history or
    narrowed to nothing would not be reading the candidate.
    """
    for ref in (f"origin/{base}", base):
        resolved = host.run_capture(
            ["git", "-C", workspace, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            "gate candidate history base",
        )
        if resolved.returncode == 0 and (resolved.stdout or "").strip():
            return f"{ref}..HEAD"
    raise HostError(
        f"gate candidate history could not resolve base {base!r} in the worker workspace; "
        "the candidate's own commits cannot be identified"
    )


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
    fetch = _backend_call(host, ["git", "-C", workspace, "fetch", "origin", base], "gate base fetch")
    if fetch.returncode != 0:
        # An answered refusal (a base branch the remote does not have, a rejected credential) is
        # a determinate gate failure, exactly as it was before this call moved here.
        raise HostError(
            f"gate base fetch (git fetch) refused: {_tail((fetch.stderr or fetch.stdout or '').strip())}"
        )
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
    pre_run_sha = _head_sha(host, workspace)
    if not is_exact_sha(pre_run_sha):
        raise HostError("local gate could not capture a full pre-run HEAD object id")
    completed = host.run_capture(["bash", "-lc", command], "local gate", cwd=Path(workspace))
    post_run_sha = _head_sha(host, workspace)
    if not is_exact_sha(post_run_sha) or post_run_sha != pre_run_sha:
        detail = (
            f"HEAD changed while local validation ran ({pre_run_sha} -> {post_run_sha or '(unavailable)'})"
        )
        return GateResult(
            "red",
            "local validation did not preserve the validated HEAD",
            detail,
            fingerprint=_fingerprint("local-head-mutated", pre_run_sha, post_run_sha),
        )
    receipt = mint_gate_receipt(
        validated_sha=pre_run_sha,
        base_sha=_base_sha(
            host,
            workspace,
            host.catalog.default_branch(task["project"], task.get("workspace", {}).get("base_branch")),
        ),
        gate_mode="local",
        required_checks=[
            {
                "name": "local validation",
                "conclusion": "SUCCESS" if completed.returncode == 0 else "FAILURE",
                "url": "",
            }
        ],
        check_set_identity=command,
    )
    if completed.returncode == 0:
        return GateResult("green", "local validation passed", attestation=receipt)
    tail = (
        _tail((completed.stderr or completed.stdout or "").strip(), GATE_LOG_FRAGMENT_LINES) or "(no output)"
    )
    # Local output lacks Actions provenance and cannot classify infrastructure.
    return GateResult(
        "red", "local validation failed", tail, fingerprint=_fingerprint("local", tail), attestation=receipt
    )


# A ref update the remote refused because the branch is not where the push expected it to be.
# Under `--force-with-lease` every divergence collapses into `stale info`; the other two shapes
# are what the same divergence reads as when no lease was in play. Anything else a remote refuses
# — a protected branch, a pre-receive hook, a permission — is a determinate failure of this gate
# and stays a HostError, because it is not a statement about where the branch points.
_LEASE_REFUSED_RE = re.compile(
    r"(?im)^\s*!\s*\[(?:remote )?rejected\][^\n]*\((?:stale info|fetch first|non-fast-forward)\)\s*$"
)


def _published_ref_entry(record, branch: str) -> dict:
    """What the dispatcher last published to `branch`, or {} when it has published nothing.

    Keyed on the branch name so a record carrying an observation for another ref — a legacy
    worker branch the card was renamed away from — is no lease at all rather than a wrong one.
    """
    entry = getattr(record, "gate_published_ref", None)
    if not isinstance(entry, dict) or entry.get("branch") != branch:
        return {}
    return entry


def _remember_published_ref(host, record, branch: str, sha: str) -> None:
    """Record the object id this gate just put on the remote branch, before another tick pushes.

    This is the lease the next publication is fenced against, and it is a durable record rather
    than a read taken at push time on purpose: a read taken now authorises whatever a foreign
    push already landed, which is precisely the thing the fence exists to refuse.
    """
    entry = {"branch": branch, "sha": sha}
    commit = getattr(host, "commit_gate_published_ref", None)
    if callable(commit):
        commit(record, entry)
    else:
        # Focused gate hosts own no dispatcher state file, but need the same tick-to-tick identity.
        record.gate_published_ref = dict(entry)


def _remote_branch_sha(host, workspace: str, branch: str) -> str:
    """The object id `origin/<branch>` carries right now, or "" when the remote has no such ref."""
    listing = _backend_call(
        host,
        ["git", "-C", workspace, "ls-remote", "origin", f"refs/heads/{branch}"],
        "gate remote branch sha",
    )
    if listing.returncode != 0:
        raise HostError(
            "gate could not read the remote branch: "
            f"{_tail((listing.stderr or listing.stdout or '').strip())}"
        )
    first = (listing.stdout or "").strip().splitlines()
    sha = first[0].split()[0].strip() if first and first[0].split() else ""
    return sha if is_exact_sha(sha) else ""


def _contained_in_candidate(host, workspace: str, sha: str, head: str) -> bool:
    """Is `sha` already reachable from the candidate head, so publishing over it loses nothing?

    An unreadable answer is not containment: the gate refuses rather than guessing that a commit
    it cannot place is one it already has.
    """
    if not is_exact_sha(sha) or not is_exact_sha(head):
        return False
    contained = host.run_capture(
        ["git", "-C", workspace, "merge-base", "--is-ancestor", sha, head],
        "gate remote branch containment",
    )
    return contained.returncode == 0


def _push_leased(host, workspace: str, branch: str, expected: str):
    """Push the candidate under a lease on `expected` — the empty string meaning "must not exist"."""
    return _backend_call(
        host,
        [
            "git",
            "-C",
            workspace,
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected}",
            "origin",
            f"{branch}:refs/heads/{branch}",
        ],
        "gate publish branch",
    )


def _publish_branch(host, record, workspace: str, branch: str, sha: str) -> GateResult | None:
    """Publish the candidate branch under a lease, or refuse and say what moved.

    A worker held between rounds rebases, so its branch is routinely not a fast-forward of what
    the remote carries; that is the normal path this gate has to publish, and a plain push failed
    it before CI ever started (secretary-1540, card codegen-orchestrator-1213). The lease is the
    object id this dispatcher itself last published, so the rewrite it authorises is exactly the
    history the dispatcher already accounted for — and a commit someone else pushed to the same
    branch is not in that history and is refused.

    Two observations are not divergence and are re-leased once rather than refused: the branch the
    dispatcher has never published (the lease is seeded from a read, and the push still fences that
    read against the moment it lands), and a remote already contained in the candidate — a publish
    whose record write was lost, or the human repair that force-pushed this very head.

    Returns None when the branch is published, and a typed publication red otherwise. `HostError`
    still means a determinate failure that is not about where the branch points.
    """
    expected = str(_published_ref_entry(record, branch).get("sha") or "")
    leased = bool(expected)
    if not leased:
        expected = _remote_branch_sha(host, workspace, branch)
    push = _push_leased(host, workspace, branch, expected)
    if push.returncode != 0:
        text = _tail((push.stderr or push.stdout or "").strip())
        if not _LEASE_REFUSED_RE.search(text):
            raise HostError(f"gate publish branch failed: {text}")
        observed = _remote_branch_sha(host, workspace, branch)
        if leased and _contained_in_candidate(host, workspace, observed, sha):
            push = _push_leased(host, workspace, branch, observed)
            text = _tail((push.stderr or push.stdout or "").strip())
        if push.returncode != 0:
            if not _LEASE_REFUSED_RE.search(text):
                raise HostError(f"gate publish branch failed: {text}")
            return _publication_refused(branch, sha, expected, observed)
    _remember_published_ref(host, record, branch, sha)
    return None


def _publication_refused(branch: str, sha: str, expected: str, observed: str) -> GateResult:
    """The remote branch moved under the dispatcher: a red gate that never reached CI.

    It is deliberately not worded as a rejected non-fast-forward. The candidate is fine and the
    worker has nothing to rebase; someone else wrote to the card branch, and the two object ids
    that say so travel on the card. `publication` keeps it apart from a red CI run in diagnostics,
    and from an infrastructure red, which is the only class the dispatcher retries by itself.
    """
    return GateResult(
        "red",
        f"branch `{branch}` was not published: `origin/{branch}` is at "
        f"`{(observed or '(absent)')[:12]}`, not the `{(expected or '(absent)')[:12]}` this "
        "dispatcher last published — someone else pushed to the card branch, so the candidate "
        f"`{sha[:12]}` was not written over it",
        f"expected origin/{branch} = {expected or '(absent)'}\n"
        f"observed origin/{branch} = {observed or '(absent)'}\n"
        f"candidate HEAD = {sha or '(unavailable)'}",
        fingerprint=_fingerprint("publish-lease", branch, expected, observed),
        failure_class="publication",
        failure_reason="remote-branch-moved",
    )


def _github_gate(
    host, task: dict, record, workspace: str, base: str, required: list[str] | None = None
) -> GateResult:
    branch = _legacy_worker_branch(task["ref"])
    no_diff_research = _is_no_diff_research_candidate(host, task, workspace, base)
    sha = host._run(["git", "-C", workspace, "rev-parse", "HEAD"], "gate head sha").stdout.strip()
    refused = _publish_branch(host, record, workspace, branch, sha)
    if refused is not None:
        return refused
    if no_diff_research:
        repo = _name_with_owner(host, workspace)
        return _no_diff_research_gate(host, record, workspace, branch, base, repo, sha, required or [])
    _ensure_pr(host, workspace, task, record, branch, base)
    repo = _name_with_owner(host, workspace)
    rerun_run_id = str(getattr(record, "gate_infrastructure_rerun_run_id", "") or "")
    rerun_sha = str(getattr(record, "gate_infrastructure_reruns_sha", "") or "")
    if rerun_run_id and rerun_sha == sha:
        status, _conclusion = _rerun_status(host, repo, rerun_run_id)
        if status != "COMPLETED":
            return GateResult(
                "pending",
                f"CI infrastructure rerun {rerun_run_id} is {status.lower()} for `{branch}` @ `{sha[:12]}`",
                failure_class="infrastructure",
                failure_reason=str(getattr(record, "gate_infrastructure_rerun_reason", "") or ""),
                failed_run_id=rerun_run_id,
                failed_run_repo=repo,
            )
    rollup, failed, checked = _poll_ci(host, repo, sha, required or [])
    receipt = mint_gate_receipt(
        validated_sha=sha,
        base_sha=_base_sha(host, workspace, base),
        gate_mode="github",
        required_checks=[_terminal_check(item) for item in checked],
        check_set_identity=json.dumps(
            {"required": sorted(required or [_check_name(item) for item in checked])},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    short = sha[:12] or sha
    if rollup == "SUCCESS":
        return GateResult("green", f"CI green for `{branch}` @ `{short}`", attestation=receipt)
    if rollup == "FAILURE":
        job = safe_one_line((failed or {}).get("name") or (failed or {}).get("context") or "?") or "?"
        fragment = _failed_log(host, repo, failed or {})
        where = f"job «{job}»"
        if fragment.step:
            where += f', step "{safe_one_line(fragment.step)}"'
        summary = f"CI red: {where} failed on `{branch}` @ `{short}`"
        if fragment.failure_class == "infrastructure":
            summary += f"; infrastructure failure: {fragment.failure_reason}"
        log = fragment.text if fragment.available else f"log unavailable: {fragment.reason}"
        cause = fragment.text if fragment.available else f"unavailable:{fragment.reason}"
        fingerprint = _fingerprint("github", job, fragment.step, cause)
        failed_run_id = _actions_run_id(failed or {})
        return GateResult(
            "red",
            summary,
            log,
            fingerprint=fingerprint,
            failure_class=fragment.failure_class,
            failure_reason=fragment.failure_reason,
            failed_run_id=failed_run_id,
            failed_run_repo=repo,
            attestation=receipt,
        )
    return GateResult(
        "pending",
        f"CI {rollup.lower()} for `{branch}` @ `{short}` — no terminal result yet",
        attestation=receipt,
    )


def _is_no_diff_research_candidate(host, task: dict, workspace: str, base: str) -> bool:
    """The one card kind allowed to validate a base-identical tree by workflow dispatch.

    `origin/<base>` is freshly fetched by `_recover_base` for this gate call.  Code cards never
    take this path, even if their tree happens to have no file diff.
    """
    if task.get("type") != "research":
        return False
    compared = host.run_capture(
        ["git", "-C", workspace, "diff", "--quiet", f"origin/{base}", "HEAD"],
        "gate no-diff research candidate",
    )
    if compared.returncode == 0:
        return True
    if compared.returncode == 1:
        return False
    raise HostError(
        "gate could not compare the research candidate with fetched base: "
        f"{_tail((compared.stderr or compared.stdout or '').strip())}"
    )


def _workflow_dispatch_entry(record) -> dict:
    entry = getattr(record, "gate_workflow_dispatch", None)
    return entry if isinstance(entry, dict) else {}


def _remember_workflow_dispatch(host, record, entry: dict) -> None:
    """Persist the dispatcher-owned request before another tick could dispatch it again."""
    commit = getattr(host, "commit_gate_workflow_dispatch", None)
    if callable(commit):
        commit(record, entry)
    else:
        # Focused gate hosts do not own a dispatcher state file, but still need the same tick-to-
        # tick identity behaviour.
        record.gate_workflow_dispatch = dict(entry)


def _dispatch_workflow(host, workspace: str, repo: str, branch: str) -> None:
    dispatched = _backend_call(
        host,
        ["gh", "workflow", "run", _NO_DIFF_WORKFLOW, "--ref", branch, "-R", repo],
        "gate workflow dispatch",
        cwd=Path(workspace),
    )
    if dispatched.returncode != 0:
        raise HostError(
            f"gate workflow dispatch failed: {_tail((dispatched.stderr or dispatched.stdout or '').strip())}"
        )


def _workflow_run_list(host, repo: str, branch: str) -> dict | None:
    completed = _backend_call(
        host,
        [
            "gh",
            "run",
            "list",
            "--workflow",
            _NO_DIFF_WORKFLOW,
            "--branch",
            branch,
            "--event",
            "workflow_dispatch",
            "--limit",
            "1",
            "-R",
            repo,
            "--json",
            _WORKFLOW_RUN_FIELDS,
        ],
        "gate workflow run list",
    )
    if completed.returncode != 0:
        raise HostError(
            f"gate workflow run list failed: {_tail((completed.stderr or completed.stdout or '').strip())}"
        )
    try:
        runs = json.loads((completed.stdout or "").strip() or "[]")
    except ValueError as exc:
        raise HostError("gate workflow run list returned invalid JSON") from exc
    if not isinstance(runs, list) or not runs:
        return None
    return runs[0] if isinstance(runs[0], dict) else None


def _workflow_run_view(host, repo: str, run_id: str) -> dict:
    completed = _backend_call(
        host,
        ["gh", "run", "view", run_id, "-R", repo, "--json", _WORKFLOW_RUN_FIELDS],
        "gate workflow run view",
    )
    if completed.returncode != 0:
        raise HostError(
            f"gate workflow run view failed: {_tail((completed.stderr or completed.stdout or '').strip())}"
        )
    try:
        run = json.loads((completed.stdout or "").strip())
    except ValueError as exc:
        raise HostError("gate workflow run view returned invalid JSON") from exc
    if not isinstance(run, dict):
        raise HostError("gate workflow run view returned no run")
    return run


def _no_diff_research_gate(
    host, record, workspace: str, branch: str, base: str, repo: str, sha: str, required: list[str]
) -> GateResult:
    """Execute the repository workflow, then bind its terminal check result to `sha`.

    Actions does not return the run id from a workflow-dispatch request.  We therefore record the
    successful request, discover one workflow-dispatch run on the worker branch, and pin future
    polls to it.  Its `headSha` and the existing commit-SHA check reader both have to agree before
    the normal GitHub receipt can become green.
    """
    entry = _workflow_dispatch_entry(record)
    if entry.get("sha") != sha or entry.get("workflow") != _NO_DIFF_WORKFLOW:
        _dispatch_workflow(host, workspace, repo, branch)
        entry = {"sha": sha, "workflow": _NO_DIFF_WORKFLOW, "run_id": ""}
        _remember_workflow_dispatch(host, record, entry)

    run_id = str(entry.get("run_id") or "")
    run = _workflow_run_view(host, repo, run_id) if run_id else _workflow_run_list(host, repo, branch)
    if run is None:
        return GateResult(
            "pending",
            f"CI workflow dispatch for `{branch}` @ `{sha[:12]}` has no Actions run yet",
        )
    found_id = str(run.get("databaseId") or "")
    found_sha = str(run.get("headSha") or "")
    if not found_id:
        raise HostError("gate workflow dispatch run has no database id")
    if found_sha != sha:
        return GateResult(
            "red",
            f"CI workflow dispatch run {found_id} is for `{found_sha[:12] or 'unavailable'}`, not candidate `{sha[:12]}`",
            fingerprint=_fingerprint("github-workflow-dispatch-sha", found_id, found_sha, sha),
            failure_reason="workflow-dispatch-head-sha-mismatch",
        )
    if found_id != run_id:
        entry = {"sha": sha, "workflow": _NO_DIFF_WORKFLOW, "run_id": found_id}
        _remember_workflow_dispatch(host, record, entry)
    status = str(run.get("status") or "").upper()
    conclusion = str(run.get("conclusion") or "").upper()
    if status != "COMPLETED":
        return GateResult(
            "pending",
            f"CI workflow dispatch run {found_id} is {status.lower() or 'pending'} for `{branch}` @ `{sha[:12]}`",
        )

    rollup, failed, checked = _poll_ci(host, repo, sha, required)
    receipt = mint_gate_receipt(
        validated_sha=sha,
        base_sha=_base_sha(host, workspace, base),
        gate_mode="github",
        required_checks=[_terminal_check(item) for item in checked],
        check_set_identity=json.dumps(
            {"required": sorted(required or [_check_name(item) for item in checked])},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    short = sha[:12] or sha
    if rollup == "SUCCESS" and conclusion == "SUCCESS":
        return GateResult(
            "green",
            f"CI workflow dispatch run {found_id} green for `{branch}` @ `{short}`",
            attestation=receipt,
        )
    if rollup == "FAILURE":
        job = safe_one_line((failed or {}).get("name") or (failed or {}).get("context") or "?") or "?"
        fragment = _failed_log(host, repo, failed or {})
        where = f"job «{job}»"
        if fragment.step:
            where += f', step "{safe_one_line(fragment.step)}"'
        log = fragment.text if fragment.available else f"log unavailable: {fragment.reason}"
        cause = fragment.text if fragment.available else f"unavailable:{fragment.reason}"
        return GateResult(
            "red",
            f"CI workflow dispatch run {found_id} red: {where} failed on `{branch}` @ `{short}`",
            log,
            fingerprint=_fingerprint("github", job, fragment.step, cause),
            failure_class=fragment.failure_class,
            failure_reason=fragment.failure_reason,
            failed_run_id=found_id,
            failed_run_repo=repo,
            attestation=receipt,
        )
    if conclusion != "SUCCESS":
        return GateResult(
            "red",
            f"CI workflow dispatch run {found_id} concluded {conclusion.lower() or 'without a result'} for `{branch}` @ `{short}`",
            fingerprint=_fingerprint("github-workflow-dispatch-conclusion", found_id, conclusion),
            attestation=receipt,
        )
    return GateResult(
        "pending",
        f"CI workflow dispatch run {found_id} completed but checks are {rollup.lower()} for `{branch}` @ `{short}`",
        attestation=receipt,
    )


def _actions_run_id(item: dict) -> str:
    match = _RUN_URL_RE.search(
        str(
            item.get("details_url")
            or item.get("html_url")
            or item.get("targetUrl")
            or item.get("target_url")
            or ""
        )
    )
    return match.group(2) if match else ""


def rerun_failed_ci(host, result: GateResult) -> None:
    """Ask Actions to rerun the failed jobs of the exact run that produced an infra red."""
    run_id = result.failed_run_id
    repo = result.failed_run_repo
    if not run_id or not repo:
        raise HostError("infrastructure gate red cannot be rerun: failed Actions run is unavailable")
    completed = _backend_call(
        host, ["gh", "run", "rerun", "--failed", run_id, "-R", repo], "gate rerun failed CI"
    )
    if completed.returncode != 0:
        raise HostError(
            f"gate rerun failed CI: {_tail((completed.stderr or completed.stdout or '').strip())}"
        )


def _rerun_status(host, repo: str, run_id: str) -> tuple[str, str]:
    """Read the rerun's current attempt, not the terminal check-runs from its prior attempt."""
    completed = _backend_call(
        host,
        ["gh", "run", "view", run_id, "-R", repo, "--json", "status,conclusion"],
        "gate infrastructure rerun status",
    )
    if completed.returncode != 0:
        raise HostError(
            f"gate infrastructure rerun status failed: {_tail((completed.stderr or completed.stdout or '').strip())}"
        )
    try:
        payload = json.loads((completed.stdout or "").strip())
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not str(payload.get("status") or ""):
        raise HostError("gate infrastructure rerun status returned no run state")
    return str(payload.get("status") or "").upper(), str(payload.get("conclusion") or "").upper()


def _name_with_owner(host, workspace: str) -> str:
    completed = _backend_call(
        host,
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        "gate repo view",
        cwd=Path(workspace),
    )
    name = (completed.stdout or "").strip()
    if completed.returncode != 0 or not name:
        # The answer travels with the failure: a message that drops the tool's own words leaves
        # nothing behind to say what the backend said.
        detail = _tail((completed.stderr or completed.stdout or "").strip()) or "(no output)"
        raise HostError(f"gate could not resolve the repository name: {detail}")
    return name


def _pr_title(task: dict, branch: str) -> str:
    """`<ref>: <card title>` — the one line a reader of `main`'s history gets."""
    ref = safe_one_line(task.get("ref") or "", limit=80) or branch
    title = safe_one_line(task.get("title") or "", limit=180)
    # GitHub refuses a title longer than 256 characters, and a card title is free text.
    return (f"{ref}: {title}" if title else ref)[:240].rstrip()


def _pr_section(text: object) -> str:
    """One board-sourced block on its way into a PR body: scrubbed and bounded, or empty."""
    cleaned = scrub_host_output(str(text or "")).replace("\r\n", "\n").strip()
    if len(cleaned) <= PR_BODY_SECTION_CHARS:
        return cleaned
    return cleaned[:PR_BODY_SECTION_CHARS].rstrip() + "\n\n… (truncated; the full text is on the card)"


def _pr_digest(title: str, body: str) -> str:
    """Digest over a title and body exactly as given, byte for byte.

    Both, because the gate writes the title too. Deliberately no canonicalisation: stripping
    trailing whitespace hides a Markdown hard line break, and any normalisation is an unmeasured
    assumption about the backend that widens what the gate will silently replace. The round-trip
    problem is solved by digesting what GitHub returns instead — see `_remember_pr`.
    """
    payload = f"{title}\n\n{body}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pr_body(task: dict, branch: str, base: str) -> str:
    """The PR description, built deterministically from the card and the worker's own report.

    Every source is optional and a missing one is omitted; no model is asked anything, so the same
    inputs always render the same body and `_refresh_pr` can compare its way to a no-op. Nothing
    marks the text as the gate's: a marker in a body proves nothing about who wrote the body
    around it, so authorship is kept outside the pull request, in `record.gate_pr_authorship`.
    """
    ref = safe_one_line(task.get("ref") or "", limit=80) or branch
    card_title = safe_one_line(task.get("title") or "", limit=180)
    heading = f"**Card:** `{ref}`" + (f" — {card_title}" if card_title else "")
    parts = [heading, f"**Branch:** `{branch}` → `{base}`"]
    statement = _pr_section(task.get("description"))
    if statement:
        parts += ["", "## What the card asks for", "", statement]
    report = _pr_section(_last_marker_body(task, "report:done") or "")
    if report:
        parts += ["", "## What the worker reports", "", report]
    parts += ["", "---", "", _PR_BODY_FOOTER]
    return "\n".join(parts)


def _pr_authorship(record) -> dict:
    """What the gate durably recorded about the last pull request it wrote for this card.

    `{"number": <pr>, "digest": <sha256 over the title and body it sent>}`, or empty. Empty is the
    safe answer and always means "not the gate's": the gate can only claim a text it can show it
    wrote.
    """
    entry = getattr(record, "gate_pr_authorship", None)
    return entry if isinstance(entry, dict) else {}


def _remember_pr(host, workspace: str, record, number: int, title: str, body: str) -> None:
    """Record that the gate wrote this pull request, digesting what GitHub hands back.

    Two digests, answering two different questions:

        - `digest` is taken over the title and body **read back from GitHub** after the accepted
          write. Ownership is decided against this byte for byte, so no assumption is needed about
          what a round trip changes.
        - `sent` is taken over the text the gate sent, which is what a later tick compares a freshly
          assembled rendering against to decide whether anything needs re-sending.

    Written only after the backend confirmed the write. If the read-back fails nothing is recorded
    at all: the pull request simply stops being the gate's and keeps the text it has.
    """
    stored = _pr_view(host, workspace, number)
    if stored is None:
        return
    host.commit_gate_pr_authorship(
        record,
        {
            "number": int(number),
            "digest": _pr_digest(str(stored.get("title") or ""), str(stored.get("body") or "")),
            "sent": _pr_digest(title, body),
        },
    )


def _gate_owns_pr(record, number: int, title: str, body: str) -> bool:
    """May the gate replace this pull request's title and body?

    Only when its own record says it wrote exactly that text on exactly that pull request. Nothing
    about the answer comes from the text itself — no marker, digest or phrasing inside a body can
    establish who wrote it. A pull request opened before this record existed therefore never
    becomes the gate's, and there is deliberately no migration and no operator override.

    Three ways to answer no, all the same rule: no record, a record about a different pull request,
    or a record whose digest no longer describes what GitHub returns. A stale description costs a
    reader some context; overwriting a person's text costs them their words.
    """
    entry = _pr_authorship(record)
    digest = str(entry.get("digest") or "")
    if not digest or int(entry.get("number") or 0) != int(number):
        return False
    return digest == _pr_digest(title, body)


def _pr_view(host, workspace: str, number: int) -> dict | None:
    """Current title and body of PR `number`, or None when the backend would not say.

    None is deliberately not distinguished from "unreadable": a PR whose current text the gate
    cannot read is one it leaves alone.
    """
    try:
        completed = _backend_call(
            host,
            ["gh", "pr", "view", str(number), "--json", "title,body"],
            "gate pr view",
            cwd=Path(workspace),
        )
    except GateTransportError:
        return None
    if completed.returncode != 0:
        return None
    try:
        data = json.loads((completed.stdout or "").strip() or "{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _refresh_pr(host, workspace: str, record, number: int, title: str, body: str) -> None:
    """Bring an already-open PR's title and body up to what the card can describe now.

    Three rules bound it, in the order that spends the fewest calls:

        - a pull request the gate has no record of writing is not even read;
        - one whose current title and body no longer reproduce the recorded digest was changed by
          somebody, and the record is left as it is so it is never reclaimed;
        - text already identical to what the gate last sent is not re-sent. That comparison uses
          the record's `sent` digest, not its ownership digest, which answers a different question.

    Every failure here is swallowed: a card whose CI is green must not be bounced because GitHub
    would not accept an edit to its prose. The create path keeps its old behaviour, since a PR that
    never opens means the `pull_request` CI never runs.
    """
    entry = _pr_authorship(record)
    recorded = str(entry.get("digest") or "")
    if not recorded or int(entry.get("number") or 0) != int(number):
        return
    if _pr_digest(title, body) == str(entry.get("sent") or ""):
        return
    current = _pr_view(host, workspace, number)
    if current is None:
        return
    if not _gate_owns_pr(record, number, str(current.get("title") or ""), str(current.get("body") or "")):
        return
    try:
        completed = _backend_call(
            host,
            ["gh", "pr", "edit", str(number), "--title", title, "--body", body],
            "gate pr edit",
            cwd=Path(workspace),
        )
    except GateTransportError:
        return
    if completed.returncode != 0:
        # GitHub refused the edit, so the pull request still carries the recorded text and the
        # record still describes it: the next tick tries again rather than disowning the PR.
        return
    _remember_pr(host, workspace, record, number, title, body)


def _ensure_pr(host, workspace: str, task: dict, record, branch: str, base: str) -> None:
    """Ensure an open PR from the worker branch into `base` exists, and that it describes the task.

    The PR exists so the project's `pull_request` CI runs — a bare feature-branch push fires
    nothing on the typical `on: [push:main, pull_request]` workflow — and its title lands in the
    merge commit. Idempotent: an already-open PR is reused and refreshed in place. Only a PR this
    call actually created is recorded as the gate's; anything else stays untouchable.
    """
    title = _pr_title(task, branch)
    body = _pr_body(task, branch, base)
    number = _open_pr_number(host, workspace, branch)
    if number is not None:
        _refresh_pr(host, workspace, record, number, title, body)
        return
    created = _backend_call(
        host,
        ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body],
        "gate pr create",
        cwd=Path(workspace),
    )
    if created.returncode == 0:
        _remember_created_pr(host, workspace, record, branch, title, body)
        return
    text = (created.stderr or created.stdout or "").strip()
    try:
        # gh refusing to duplicate a PR, or a concurrent tick that opened one first: the create
        # answered "no", and it is only tolerated when the backend also answers that a PR is open.
        if _open_pr_number(host, workspace, branch) is not None:
            return
    except HostError as exc:
        if isinstance(exc, GateTransportError):
            raise
        raise HostError(
            f"gate could not open a PR for {branch!r}: {_tail(text)}; and the open-PR probe failed too: {exc}"
        ) from None
    raise HostError(f"gate could not open a PR for {branch!r}: {_tail(text)}")


def _remember_created_pr(host, workspace: str, record, branch: str, title: str, body: str) -> None:
    """Record the pull request this gate just opened, by asking the backend which number it got.

    The number is read back through `--json number` rather than parsed out of the URL `gh pr
    create` prints. A backend that will not answer costs the description, not the gate: without a
    number there is no record, so the gate treats its own pull request as a person's.
    """
    try:
        number = _open_pr_number(host, workspace, branch)
    except HostError:
        return
    if number is None:
        return
    _remember_pr(host, workspace, record, number, title, body)


def _open_pr_number(host, workspace: str, branch: str) -> int | None:
    """Number of the open PR whose head is `branch`, or None when the backend answered that none is
    open. `gh pr list` exits 0 with empty output when nothing matches, so no-PR is not confused
    with a gh failure.

    "No PR is open" is a positive fact about the backend's state, so it may only be returned for an
    answer; a tool that never got through raises out of `_backend_call` instead.
    """
    completed = _backend_call(
        host,
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number", "-q", ".[0].number"],
        "gate pr list",
        cwd=Path(workspace),
    )
    if completed.returncode != 0:
        raise HostError(f"gate pr list failed: {_tail((completed.stderr or completed.stdout or '').strip())}")
    text = (completed.stdout or "").strip()
    try:
        return int(text) if text else None
    except ValueError:
        return None


def _poll_ci(
    host, repo: str, sha: str, required: list[str] | None = None
) -> tuple[str, dict | None, list[dict]]:
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
    return sorted(
        selected, key=lambda item: (_check_name(item), str(item.get("id") or item.get("context") or ""))
    )


def _terminal_check(item: dict) -> dict[str, str]:
    conclusion = str(item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
    return {
        "name": safe_one_line(_check_name(item)),
        "conclusion": conclusion,
        "url": safe_one_line(
            item.get("details_url")
            or item.get("html_url")
            or item.get("target_url")
            or item.get("targetUrl")
            or ""
        ),
    }


def _head_sha(host, workspace: str) -> str:
    if not workspace:
        return ""
    try:
        return host._run(
            ["git", "-C", workspace, "rev-parse", "HEAD"], "gate attestation head"
        ).stdout.strip()
    except HostError:
        return ""


def _base_sha(host, workspace: str, base: str) -> str:
    if not workspace or not base:
        return ""
    try:
        return host._run(
            ["git", "-C", workspace, "rev-parse", f"origin/{base}"], "gate attestation base"
        ).stdout.strip()
    except HostError:
        return ""


def _gh_api(host, path: str, *, jq: str):
    completed = _backend_call(host, ["gh", "api", path, "--jq", jq], "gate gh api")
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
    """Overall CI state: PENDING while any job is still running (even next to an already-failed one —
    a flaky-looking early failure must not bounce the card before the suite finishes), then FAILURE
    (with the first failing entry) once every job is terminal, SUCCESS if none failed, or NONE when
    there are no checks at all.

    With a declared `required` set the gate judges by those names only: other entries are ignored,
    and a required name with no entry on the sha at all counts as pending.
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

    `--log-failed` interleaves every failed job in the run, aggregator jobs included, so the
    fragment is scoped to the job the rollup reported. Within it, the window ends at the last
    `##[error]` line that is not the runner's generic completion echo, which carries the same
    marker as a real cause; a job whose error was never marked at all falls back to its own tail.
    """
    match = _RUN_URL_RE.search(
        str(
            item.get("details_url")
            or item.get("html_url")
            or item.get("targetUrl")
            or item.get("target_url")
            or ""
        )
    )
    if not match:
        return _LogFragment(available=False, reason="the entry is not an Actions run (no run link)")
    run_id = match.group(2)
    try:
        completed = _backend_call(
            host, ["gh", "run", "view", run_id, "-R", repo, "--log-failed"], "gate failed log"
        )
    except GateTransportError as exc:
        # Failed-log silence degrades the excerpt; it does not reopen an answered red verdict.
        return _LogFragment(available=False, reason=f"the log could not be fetched: {exc}")
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
    failure_class, failure_reason = _classify_failed_step(step, text)
    return _LogFragment(
        available=True,
        step=step,
        text=text,
        failure_class=failure_class,
        failure_reason=failure_reason,
    )


def _classify_failed_step(step: str, text: str) -> tuple[str, str]:
    """Classify an Actions red from its failed step and service evidence.

    This is intentionally an allowlist.  In particular, a failing ``Set up job`` does not become
    infra merely because of its name: the logged failure must show an action-download 5xx, or an
    unavailable runner.  The return value is persisted by the dispatcher before a card comment is
    rendered, so card prose cannot affect a later retry decision.
    """
    step = step.strip()
    lowered_step = step.casefold()
    if "set up job" in lowered_step and _ACTION_DOWNLOAD_5XX_RE.search(text):
        return "infrastructure", "action-download-http-5xx"
    if "set up docker buildx" in lowered_step and _REGISTRY_UNAVAILABLE_RE.search(text):
        return "infrastructure", "buildx-registry-unavailable"
    if any(
        token in lowered_step
        for token in (
            "initialize containers",
            "initialize container",
            "pull image",
            "pulling image",
            "docker pull",
            "load image",
        )
    ) and _REGISTRY_UNAVAILABLE_RE.search(text):
        return "infrastructure", "image-registry-unavailable"
    if "set up job" in lowered_step and _RUNNER_UNAVAILABLE_RE.search(text):
        return "infrastructure", "runner-unavailable"
    return "substantive", ""
