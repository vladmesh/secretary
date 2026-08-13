"""Mechanical validation gate (secretary-633): the cheap-to-expensive layer 1 the dispatcher runs
between worker-done and the LLM reviewer, and again right before a merge.

A project declares where its gate runs through its adapter's `validation.ci`:

  local  — run `validation.command` in the worker workspace; exit 0 is green, non-zero is red.
  github — publish the worker branch, ensure an open PR into the project's base branch (so the
           typical `on: [push:main, pull_request]` workflow actually fires — a bare feature-branch
           push triggers nothing), described from the card and the worker's own done report and
           kept up to date on every later tick (secretary-1439), then poll GitHub CI for the
           branch head sha; SUCCESS is green,
           FAILURE is red, PENDING/NONE is pending (a check still running, or none posted yet —
           "CI did not start", deliberately not confused with "CI is red"). `validation
           .required_checks` narrows the rollup to those check names; without it every check on
           the sha counts.
  none   — no mechanical gate; the card goes straight to review (unchanged pre-633 behaviour).

Candidate history is checked in every mode, `none` included, before anything is published or
validated: a commit message carrying a forbidden AI co-author trailer is a red gate with a local
repair, never a push followed by a history rewrite (secretary-1401).

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
from dataclasses import dataclass
from pathlib import Path

from secretary.candidate_history import Commit, ai_attributions, parse_shas, repair_message
from secretary.dispatcher_helpers import (
    _last_marker_body,
    _legacy_worker_branch,
    _tail,
    safe_one_line,
    scrub_host_output,
)
from secretary.dispatcher_gate_receipt import is_exact_sha, mint_gate_receipt
from secretary.dispatcher_types import GateTransportError, HostError

# How long a github CI rollup may sit non-terminal (PENDING/NONE) before the pending watchdog
# escalates the card to Blocked — a required check nothing ever posts, a job waiting on manual
# environment approval, or a removed workflow would otherwise leave the card unwatched forever.
GATE_PENDING_STALL_SECONDS = int(os.environ.get("SECRETARY_GATE_PENDING_STALL_SECONDS", str(6 * 3600)))

# Single knob for how much of a failed gate's log reaches the worker (secretary-766): both the
# local-gate stderr/stdout tail and the github-gate `--log-failed` fragment size read this, so
# there is one place to widen or narrow the excerpt instead of a magic number per call site.
GATE_LOG_FRAGMENT_LINES = int(os.environ.get("SECRETARY_GATE_LOG_FRAGMENT_LINES", "40"))

# How many consecutive ticks the gate backend may fail to answer before the card is blocked
# (secretary-1164). A blip costs the card a tick, not a round; a backend that is genuinely gone
# still reaches a human instead of leaving the card in Validate forever.
GATE_TRANSPORT_MAX_ATTEMPTS = max(1, int(os.environ.get("SECRETARY_GATE_TRANSPORT_MAX_ATTEMPTS", "5")))

# How much of one board source (the card statement, the worker's done report) reaches the PR body
# (secretary-1439). The PR describes the change and points at the card; it is not a second copy of
# the board, and a card that carries a whole design document must not push the body towards
# GitHub's own size ceiling.
PR_BODY_SECTION_CHARS = int(os.environ.get("SECRETARY_PR_BODY_SECTION_CHARS", "4000"))

# Hidden first line of every PR body this gate writes, carrying a digest over the exact title and
# body it sent (secretary-1439). It is the whole authorship test on the refresh path: the gate may
# replace text it can prove is byte-for-byte its own last write, and nothing else. A bare marker
# would not do — a person who edits a gate-written body normally keeps the surrounding Markdown,
# hidden comment included, and a containment test would read their prose as the gate's own and
# overwrite it. Any edit to either the title or the body breaks the digest, and the gate then
# treats the PR as a person's and leaves it alone forever.
#
# The digest is unkeyed, so it proves "unedited since the gate wrote it", not "written by the
# gate": anyone able to edit the PR can compute a matching digest over text of their own and hand
# the gate permission to overwrite it. Establishing genuine authorship would need an authenticated
# provenance boundary (the PR's edit history or a signing secret GitHub cannot see), which no
# amount of reading the body text can supply. The threat this closes is the ordinary human edit,
# not a deliberate spoof.
_PR_MARKER_PREFIX = "<!-- secretary-gate-pr sha256:"
_PR_MARKER_RE = re.compile(r"^<!-- secretary-gate-pr sha256:([0-9a-f]{32}) -->\n?")
# The fixed stub every gate before secretary-1439 wrote, as a format over the branch and the ref.
# It predates the marker, and it is unmistakably machine text — 26 of the last 30 merged PRs
# carried exactly it — so a PR still holding exactly it is upgraded rather than frozen behind the
# manual-body rule. Exactly it: a prefix test would also swallow a body a person began by quoting
# the stub and then wrote under.
_PR_LEGACY_STUB = (
    "Automatic PR for worker branch `{branch}` of task {ref}. "
    "Opened by the CI gate so that the pull_request CI runs."
)
# Closing line of every gate-written body: says who opened the PR and why, so a reader who lands
# on it from the merge commit knows the PR is the pipeline's and not a human's proposal.
_PR_BODY_FOOTER = (
    "Opened by the secretary CI gate so that the `pull_request` workflow runs. "
    "Title and body are built from the card and the worker's done report."
)

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
# How a failed backend command says that an answer *did* arrive (secretary-1164).
#
# The rule is positive and the default is silence: a question put to the backend counts as
# answered only when the tool prints one of the shapes below, and anything else it prints is a
# question that got no answer. That direction is deliberate. Guessing "no answer" costs a few
# retries and a Blocked reason that names the transport and quotes the tool; guessing "answer"
# costs an immediate wrong Blocked on a moment of bad network, which is the incident this card
# exists for. Two rounds of this card were red for phrases missing from a list of failures; there
# is no such list any more, because the failures are open-ended and the answers are not.
#
# Every shape below was taken from the binaries this gate runs, on this machine (gh 2.45.0,
# git 2.43.0). The captures are in the tests next to them.
#
# 1. An HTTP status the tool quotes. Only the backend can produce one.
#      gh api        -> gh: Not Found (HTTP 404)
#      gh run view   -> failed to get run: HTTP 404: Not Found (https://api.github.com/...)
#      git over HTTPS-> error: RPC failed; HTTP 502 ...
#                       fatal: unable to access '...': The requested URL returned error: 503
#    A 5xx is the exception: the backend failed to serve an answer, and this card counts that as
#    transport (acceptance criterion 1 names it).
_HTTP_STATUS_RE = re.compile(r"(?i)\bhttp(?:/\d(?:\.\d)?)?[ /](\d{3})\b|returned error:\s*(\d{3})\b")
# 2. A GraphQL error: gh rendering an API response body it parsed. This is what the gate's own
#    calls answer with when the repository or the PR is the problem.
#      gh repo view  -> GraphQL: Could not resolve to a Repository with the name '...'. (repository)
#      gh pr list    -> GraphQL: Could not resolve to a Repository with the name '...'. (repository)
#      gh pr create  -> pull request create failed: GraphQL: A pull request already exists for ...
#    A raw response body reaching the text counts for the same reason.
# 3. git's push report, which exists only because the remote answered: the per-ref status table it
#    prints from the server's report-status, and any line the server itself sent.
#      git push      ->  ! [rejected]        main -> main (fetch first)
#                        remote: policy: branch is protected
#                        ! [remote rejected] main -> main (pre-receive hook declined)
_ANSWERED_RE = re.compile(
    r'(?im)(\bgraphql:\s|^\s*remote:\s|!\s*\[(?:rejected|remote rejected|deleted|no match)\]'
    r'|^\s*\{\s*"(?:errors|message)")'
)


def _backend_answered(text: str) -> bool:
    """Did the gate's backend answer this failed backend command?

    Only `_backend_call` asks. An HTTP status decides first (5xx is the backend failing to answer,
    not an answer); otherwise one of the answer shapes above must be present. Text that matches
    none of them — a Go `url.Error`, a curl or GnuTLS transport message, gh's own
    "error connecting to", an empty stderr, or a wording nobody has seen yet — is a question that
    got no answer.
    """
    text = (text or "").strip()
    if not text:
        return False
    status = _HTTP_STATUS_RE.search(text)
    if status:
        code = status.group(1) or status.group(2)
        return not code.startswith("5")
    return bool(_ANSWERED_RE.search(text))


def _backend_call(host, args: list[str], label: str, *, cwd: Path | None = None):
    """Ask the gate's backend, and be the one place that decides no answer came back.

    Every question this gate puts to a remote — the base fetch, the branch push, the PR probe,
    the PR create, the repository name, the check rollup, the failed-job log — goes through here,
    and nothing else does. A path that does not call this cannot raise `GateTransportError`, which
    is what keeps the local gate's own hanging command (a determinate answer about the branch)
    out of the transport class no matter how its message happens to read.

    Returns the CompletedProcess, including a non-zero one that carries an answer; raises
    GateTransportError when the tool could not run to completion or its output says it never got
    through. The tool's own text always travels with the failure — a caller that swallows it
    leaves the classification nothing to read (secretary-1164 review).
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
    if _backend_answered(text):
        return completed
    raise GateTransportError(f"{label} got no answer: {text or '(no output)'}")


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
    """Run the mechanical gate for `task` in the worker workspace.

    Raises `GateTransportError` when a question put to the backend got no answer — decided by
    `_backend_call`, which every such question goes through and nothing else does. Raises plain
    `HostError` for any determinate gate failure: a missing workspace, a misconfigured mode, a
    repository the backend answered it does not know, a local validation command that hung.
    Returns a GateResult otherwise, red answers included.
    """
    if getattr(host, "mode", "real") == "noop":
        # A noop proves no command ran.  It may preserve dispatcher control flow, but must never
        # look like reusable validation evidence.
        return GateResult("green", "noop gate")
    ci = _validation(host, task).get("ci") or "none"
    workspace = record.workspace
    # Every mode needs the checkout now, `none` included: the candidate-history preflight below is
    # not mechanical validation and `none` does not opt out of it, so a workspace that cannot be
    # read is a gate that cannot answer rather than a boundary quietly skipped (secretary-1401).
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
    # Ahead of every publication and of every validation command, and ahead of the `none` exit: a
    # project without a mechanical gate still merges its candidate, so it is exactly as unable to
    # repair a published AI trailer as one with a gate.
    history = _candidate_history_gate(host, workspace, base)
    if history is not None:
        return history
    if ci == "none":
        # `none` is an explicit absence of mechanical validation, not an all-green check set.
        return GateResult("green", "ci none: mechanical gate skipped")
    if ci == "local":
        return _local_gate(host, task, record, workspace)
    if ci == "github":
        return _github_gate(host, task, workspace, base, _required_checks(host, task))
    raise HostError(f"unsupported validation ci mode {ci!r}; expected local, github or none")


def _candidate_history_gate(host, workspace: str, base: str) -> GateResult | None:
    """Reject forbidden AI attribution in `base..HEAD` before anything is published.

    This is the dispatcher's own check, not a worker's: it runs on the gate path every candidate
    crosses, ahead of the branch push in `_github_gate`, ahead of the local suite, and ahead of the
    `none` exit, so a violation costs a rework round rather than a rewrite of published history
    (secretary-1401, from the two AI-trailer commits published in `sprint:1300`).

    Only the commit messages are read. Generated workspace packets (`TASK.md`, `REVIEW.md`) are
    git-ignored operational projections and never enter this range, whatever they happen to quote.

    The messages are read one commit at a time, keyed on object ids that were listed separately.
    Framing several untrusted messages into one stream is what a candidate can attack: a message
    may contain any byte, delimiter bytes included, and a stream split on such a byte can be made
    to parse into records that no longer carry the trailer. There is no delimiter here to forge —
    `%H` cannot be influenced by message text, `parse_shas` refuses anything that is not an object
    id, and each `git log -1 --format=%B` hands back exactly one message as its whole stdout.

    Nothing about a range that will not answer is a pass: an unreadable listing, an unreadable
    message, or a listing that is not object ids raises `HostError`, and the card is blocked over a
    gate that could not say what it would publish rather than publishing unchecked history.
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
            raise HostError(
                f"gate candidate history could not be decoded for {sha[:12]}"
            ) from exc
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

    `origin/<base>` is the right base wherever the gate has just fetched it. A `none` project never
    fetches, and its checkout may only have the local branch, so the local ref is the fallback and
    a base that resolves neither way raises: a preflight that silently widened its range to the
    whole history, or narrowed it to nothing, would not be reading the candidate.
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
            f"gate base fetch failed: {_tail((fetch.stderr or fetch.stdout or '').strip())}"
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
            f"HEAD changed while local validation ran ({pre_run_sha} -> "
            f"{post_run_sha or '(unavailable)'})"
        )
        return GateResult(
            "red", "local validation did not preserve the validated HEAD", detail,
            fingerprint=_fingerprint("local-head-mutated", pre_run_sha, post_run_sha),
        )
    receipt = mint_gate_receipt(
        validated_sha=pre_run_sha,
        base_sha=_base_sha(host, workspace, host.catalog.default_branch(task["project"], task.get("workspace", {}).get("base_branch"))),
        gate_mode="local",
        required_checks=[{"name": "local validation", "conclusion": "SUCCESS" if completed.returncode == 0 else "FAILURE", "url": ""}],
        check_set_identity=command,
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
    push = _backend_call(
        host, ["git", "-C", workspace, "push", "origin", f"{branch}:{branch}"], "gate publish branch"
    )
    if push.returncode != 0:
        raise HostError(
            f"gate publish branch failed: {_tail((push.stderr or push.stdout or '').strip())}"
        )
    _ensure_pr(host, workspace, task, branch, base)
    sha = host._run(["git", "-C", workspace, "rev-parse", "HEAD"], "gate head sha").stdout.strip()
    repo = _name_with_owner(host, workspace)
    rollup, failed, checked = _poll_ci(host, repo, sha, required or [])
    receipt = mint_gate_receipt(
        validated_sha=sha,
        base_sha=_base_sha(host, workspace, base),
        gate_mode="github",
        required_checks=[_terminal_check(item) for item in checked],
        check_set_identity=json.dumps(
            {"required": sorted(required or [_check_name(item) for item in checked])},
            sort_keys=True, separators=(",", ":"),
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
            where += f", step \"{safe_one_line(fragment.step)}\""
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
    completed = _backend_call(
        host,
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        "gate repo view",
        cwd=Path(workspace),
    )
    name = (completed.stdout or "").strip()
    if completed.returncode != 0 or not name:
        # The answer travels with the failure: a message that drops the tool's own words leaves
        # nothing behind to say what the backend said, which is how a transport failure here used
        # to reach the dispatcher stripped of its evidence (secretary-1164 review).
        detail = _tail((completed.stderr or completed.stdout or "").strip()) or "(no output)"
        raise HostError(f"gate could not resolve the repository name: {detail}")
    return name


def _pr_title(task: dict, branch: str) -> str:
    """`<ref>: <card title>` — the one line a reader of `main`'s history gets.

    The branch name is not in it: it used to be, as `<ref>: pipeline/<ref>`, which spent the whole
    line repeating the reference and never said what the card was (secretary-1439). A card whose
    title the board could not answer falls back to the reference alone rather than to the branch.
    """
    ref = safe_one_line(task.get("ref") or "", limit=80) or branch
    title = safe_one_line(task.get("title") or "", limit=180)
    # GitHub refuses a title longer than 256 characters, and a card title is free text.
    return (f"{ref}: {title}" if title else ref)[:240].rstrip()


def _pr_section(text: object) -> str:
    """One board-sourced block on its way into a PR body: scrubbed and bounded, or empty.

    The board text is not the gate's own, and the PR is published to a remote the card is not, so
    the same redaction every other host/board excerpt goes through applies here. Newlines survive
    (unlike `safe_one_line`): a done report is prose with structure, and flattening it would make
    the body less readable than the stub it replaces.
    """
    cleaned = scrub_host_output(str(text or "")).replace("\r\n", "\n").strip()
    if len(cleaned) <= PR_BODY_SECTION_CHARS:
        return cleaned
    return cleaned[:PR_BODY_SECTION_CHARS].rstrip() + "\n\n… (truncated; the full text is on the card)"


def _canonical_text(text: object) -> str:
    """PR text as a reader sees it, free of what a round trip through GitHub changes.

    GitHub hands a body back with CRLF line endings and its own idea of trailing whitespace, so a
    byte comparison against the text the gate just sent would differ every time: it would turn
    "nothing changed" into an edit call per tick, and — since the same canonical form is what the
    ownership digest is taken over — it would make the gate disown its own writing the moment it
    read it back. Any difference that survives this is a real difference in what a reader sees.
    """
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def _pr_digest(title: str, body: str) -> str:
    """Ownership digest over the exact pair the gate sends to `gh pr edit`/`gh pr create`.

    Both, not just the body: the gate writes the title too, and a person who retitles a PR has
    written something automation must not throw away either. Taken over the canonical form so the
    gate still recognises its own text after GitHub has handed it back.
    """
    payload = f"{_canonical_text(title)}\n\n{_canonical_text(body)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _pr_stamp(title: str, content: str) -> str:
    """`content` with the marker line that lets a later tick recognise it as untouched gate text."""
    canonical = _canonical_text(content)
    return f"{_PR_MARKER_PREFIX}{_pr_digest(title, canonical)} -->\n{canonical}\n"


def _pr_body(task: dict, branch: str, base: str, title: str) -> str:
    """The PR description, built deterministically from the card and the worker's own report.

    Every source is optional and a missing one is simply omitted: the gate also runs before the
    first report exists (and again before the merge), and a description is never a reason to fail
    a candidate whose code is fine. No model is asked anything here — the same card and the same
    report always render the same body, which is what makes the refresh in `_refresh_pr` able to
    compare its way to a no-op.

    `title` is not rendered into the text; it goes into the marker's digest, so the stamp covers
    everything this gate is about to write.
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
    return _pr_stamp(title, "\n".join(parts))


def _gate_authored(title: str, body: str, legacy_stub: str) -> bool:
    """May the gate replace this PR's title and body?

    Only text it can prove is exactly what it last wrote: the marker's digest is recomputed over
    the title and the remaining body, and anything that fails to reproduce it — a paragraph a
    reviewer added under a kept marker, a retitled PR, a marker pasted into prose — is a person's
    writing and is never overwritten (secretary-1439). Two other bodies count as the gate's: the
    pre-1439 stub, exactly and only exactly, which predates the marker and which a still-open PR
    may be carrying; and nothing at all, since an empty body is nobody's text.

    What this cannot do is prove authorship against someone who wants to fool it: the digest is
    unkeyed, so a body deliberately stamped with a correct digest over its own text reads as the
    gate's. That distinction is not available from PR body text at all — it would need the PR's
    edit history or a secret GitHub never sees. This test is aimed at the honest edit, which it
    catches completely.
    """
    text = _canonical_text(body)
    if not text:
        return True
    if text == _canonical_text(legacy_stub):
        return True
    marker = _PR_MARKER_RE.match(text)
    if marker is None:
        return False
    return marker.group(1) == _pr_digest(title, text[marker.end():])


def _same_text(current: str, wanted: str) -> bool:
    """Is the PR already carrying this text, up to what a round trip through GitHub changes?"""
    return _canonical_text(current) == _canonical_text(wanted)


def _pr_view(host, workspace: str, number: int) -> dict | None:
    """Current title and body of PR `number`, or None when the backend would not say.

    None is deliberately not distinguished from "unreadable": the only caller uses this to decide
    whether an update is needed, and a PR whose current text the gate cannot read is one it leaves
    alone.
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


def _refresh_pr(host, workspace: str, number: int, title: str, body: str, legacy_stub: str) -> None:
    """Bring an already-open PR's title and body up to what the card can describe now.

    `_ensure_pr` used to return the moment a PR was open, and nothing else in the dispatcher ever
    called `gh pr edit`, so the description the gate could build on the *first* tick was the one
    the PR kept forever — including the tick before the worker had reported anything at all
    (secretary-1439). The gate runs again on every later tick and once more before the merge, so
    the same call site now carries the better text over.

    Two rules bound it. Text that is not exactly what the gate last wrote is not touched at all
    (`_gate_authored` — the marker's digest over the current title and body), and text that
    already matches is not re-sent: `_pr_body` is a pure function of the card, so a repeat tick on
    unchanged data reaches the comparison and makes no backend call.

    Every failure here is swallowed: the description is not a condition on the code, and a card
    whose CI is green must not be bounced, retried or blocked because GitHub would not accept an
    edit to its prose. The create path keeps its old behaviour — a PR that never opens means the
    `pull_request` CI never runs, which is a real gate failure.
    """
    current = _pr_view(host, workspace, number)
    if current is None:
        return
    current_body = str(current.get("body") or "")
    current_title = str(current.get("title") or "")
    if not _gate_authored(current_title, current_body, legacy_stub):
        return
    if _same_text(current_title, title) and _same_text(current_body, body):
        return
    try:
        _backend_call(
            host,
            ["gh", "pr", "edit", str(number), "--title", title, "--body", body],
            "gate pr edit",
            cwd=Path(workspace),
        )
    except GateTransportError:
        return


def _ensure_pr(host, workspace: str, task: dict, branch: str, base: str) -> None:
    """Ensure an open PR from the worker branch into `base` exists, and that it describes the task.

    The PR exists so the project's `pull_request` CI runs (a bare feature-branch push fires nothing
    on the typical `on: [push:main, pull_request]` workflow), and its title is what lands in the
    merge commit, so it is also the only thing a later reader of `main` sees about the card.

    Idempotent: an already-open PR is reused and refreshed in place, and a concurrent tick or gh
    refusing to duplicate a PR is tolerated as long as one is open.
    """
    title = _pr_title(task, branch)
    body = _pr_body(task, branch, base, title)
    number = _open_pr_number(host, workspace, branch)
    if number is not None:
        legacy = _PR_LEGACY_STUB.format(branch=branch, ref=task.get("ref") or "")
        _refresh_pr(host, workspace, number, title, body, legacy)
        return
    created = _backend_call(
        host,
        ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body],
        "gate pr create",
        cwd=Path(workspace),
    )
    if created.returncode == 0:
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
            f"gate could not open a PR for {branch!r}: {_tail(text)}; "
            f"and the open-PR probe failed too: {exc}"
        ) from None
    raise HostError(f"gate could not open a PR for {branch!r}: {_tail(text)}")


def _open_pr_number(host, workspace: str, branch: str) -> int | None:
    """Number of the open PR whose head is `branch`, or None when the backend answered that none
    is open. `gh pr list` exits 0 with empty output when nothing matches, so no-PR is not confused
    with a gh failure.

    "No PR is open" is a positive fact about the backend's state, so it may only be returned for
    an answer. A tool that never got through raises out of `_backend_call` instead: reading that
    as "there is no PR" used to send the gate on to open a second one (secretary-1164 review),
    and an answered failure is still a determinate gate failure rather than a silent None."""
    completed = _backend_call(
        host,
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number", "-q", ".[0].number"],
        "gate pr list",
        cwd=Path(workspace),
    )
    if completed.returncode != 0:
        raise HostError(
            f"gate pr list failed: {_tail((completed.stderr or completed.stdout or '').strip())}"
        )
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
        "name": safe_one_line(_check_name(item)),
        "conclusion": conclusion,
        "url": safe_one_line(item.get("details_url") or item.get("html_url") or item.get("target_url") or item.get("targetUrl") or ""),
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
    try:
        completed = _backend_call(
            host, ["gh", "run", "view", run_id, "-R", repo, "--log-failed"], "gate failed log"
        )
    except GateTransportError as exc:
        # The one backend call whose silence is deliberately not a transport failure of the gate:
        # the verdict is already red, decided by an answer that did arrive, and the log is only
        # the excerpt attached to it. Turning this into a retry would send a card whose CI has
        # genuinely failed back around the loop, so the fragment degrades and says why.
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
    return _LogFragment(available=True, job=job_name, step=step, text=text, infra=bool(_INFRA_MARK_RE.search(text)))
