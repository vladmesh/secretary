"""The post-merge CI watch: a release is finished when the base's CI for its merge has answered.

A release merge starts a CI run on the integration base. The dispatcher owns all waiting for CI, so
it records a watch the moment the merge has landed (card ref, project, base, the merge commit that
actually landed, start time) in its own production state, beside the card records and never among
them: a watched card is Done and holds no claim. Each tick reads the base's CI for that exact commit
with the gate's own machinery and resolves the watch to exactly one of four results:

- `green`: every selected check finished successfully;
- `red`: at least one failed, with the run ids, failed check names and the gate's
  infrastructure/product classification;
- `absent`: the project validates without GitHub CI, or no workflow triggers on a push to the base,
  decided when the watch is opened, without waiting;
- `timeout`: no terminal result within `SECRETARY_POST_MERGE_CI_CEILING_SECONDS` (default 3600).

Anything that is not a terminal answer about this commit (a transport error, an unreadable or
malformed `gh` answer, a check for another commit, a completed check with no conclusion, a run that
has not started) is pending until the ceiling. Nothing but a complete set of successful checks is
ever `green`.

The result is written to the watch before anything is published, so a replayed tick publishes the
same fact under the same request ids: one card event (the observer wake, see
`secretary.tasks.is_significant_card_event`) and, for a sprint card, one dispatcher comment on the
sprint. The watch is dropped only after both are on the board.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from secretary.dispatch.gate import (
    _actions_run_id,
    _check_name,
    _check_result,
    _failed_log,
    _gh_api,
    _name_with_owner,
    _required_checks,
    _rollup,
    _selected_checks,
    push_trigger_absent_reason,
)
from secretary.dispatch.helpers import safe_one_line
from secretary.dispatch.types import HostError, MergeLanding
from secretary.infra.env import positive_int
from secretary.tasks import POST_MERGE_CI_KEY, TaskError, post_merge_ci_fact

WATCHES_KEY = "post_merge_watches"
CEILING_ENV = "SECRETARY_POST_MERGE_CI_CEILING_SECONDS"
DEFAULT_CEILING_SECONDS = 3600
#: How many failed runs have their logs read for the classification; the rest are listed only.
_CLASSIFIED_RUNS = 3
_SHA_RE = re.compile(r"[0-9a-f]{40}")


def ceiling_seconds() -> int:
    return positive_int(CEILING_ENV, DEFAULT_CEILING_SECONDS)


def watches(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The live watches in the dispatcher production state, created on first use."""
    raw = payload.get(WATCHES_KEY)
    if not isinstance(raw, dict):
        raw = {}
        payload[WATCHES_KEY] = raw
    return raw


def pr_merge_commit(run: Callable[..., Any], branch: str, cwd: Path) -> str:
    """The merge commit of `branch`'s pull request, or "" when it cannot be read.

    Only a full 40-hex commit is an answer; anything else is "not known yet".
    """
    if not branch:
        return ""
    try:
        completed = run(
            ["gh", "pr", "view", branch, "--json", "mergeCommit", "-q", ".mergeCommit.oid"],
            "post-merge commit",
            cwd=cwd,
        )
    except (HostError, OSError):
        return ""
    sha = str(getattr(completed, "stdout", "") or "").strip()
    return sha if _SHA_RE.fullmatch(sha) else ""


def open_watch(
    runtime: Any,
    task: dict[str, Any],
    payload: dict[str, Any],
    landing: MergeLanding,
    *,
    workspace: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Record the watch for a merge that has just landed; a replay keeps the first one.

    The absence of any base CI is decided here, from the merged checkout, while it still exists:
    the checkout is torn down before Done.
    """
    ref = task["ref"]
    live = watches(payload)
    existing = live.get(ref)
    if isinstance(existing, dict) and not existing.get("result"):
        if not existing.get("merge_sha") and landing.sha:
            existing["merge_sha"] = landing.sha
        return existing
    if landing.ci != "github":
        absent = (
            f"the project validates with `validation.ci = {landing.ci}`, not GitHub CI, so the "
            "merge starts no CI run the dispatcher could read"
        )
    else:
        absent = push_trigger_absent_reason(workspace, landing.base) if workspace else ""
    started = time.time() if now is None else now
    watch = {
        "version": 1,
        "id": uuid.uuid4().hex[:16],
        "ref": ref,
        "project": str(task.get("project") or ""),
        "sprint": str(task.get("sprint") or ""),
        "base": landing.base,
        "merge_sha": landing.sha,
        "merge_path": landing.path,
        "branch": landing.branch,
        "ci": landing.ci,
        "started_at": started,
        "absent_reason": absent,
        "repo": "",
        "last_state": "",
        "result": None,
    }
    live[ref] = watch
    return watch


def release_merge_marker(watch: dict[str, Any]) -> dict[str, Any]:
    """The Done transition's `release_merge` data: what landed, and that its CI result follows."""
    return {
        "base": str(watch.get("base") or ""),
        "merge_sha": str(watch.get("merge_sha") or ""),
        "path": str(watch.get("merge_path") or ""),
    }


def reconcile_post_merge_watches(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, Any],
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Resolve and publish every watch that has an answer this tick."""
    live = watches(payload)
    outcomes: list[dict[str, Any]] = []
    for ref in sorted(live):
        watch = live[ref]
        if not isinstance(watch, dict):
            del live[ref]
            continue
        if not watch.get("result"):
            result = resolve(runtime, watch, now=now)
            if result is None:
                continue
            # Durable before it is published: a replay publishes this fact, never a re-read one.
            watch["result"] = result
            runtime.save_records(payload, records)
        try:
            publish(runtime, watch)
        except (TaskError, HostError, OSError, ValueError, TypeError) as exc:
            outcomes.append(
                {
                    "status": "degraded",
                    "step": "post-merge-ci",
                    "action": "post-merge-ci-publish-failed",
                    "pilot_ref": ref,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        del live[ref]
        runtime.save_records(payload, records)
        outcomes.append(
            {
                "status": "ok",
                "step": "post-merge-ci",
                "pilot_ref": ref,
                "result": watch["result"]["result"],
                "merge_sha": watch["result"].get("merge_sha", ""),
            }
        )
    return outcomes


def resolve(runtime: Any, watch: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
    """The watch's result, or None while it is pending."""
    current = time.time() if now is None else now
    waited = max(0, int(current - float(watch.get("started_at") or current)))
    if watch.get("absent_reason"):
        return _fact(watch, "absent", waited, reason=str(watch["absent_reason"]))
    read = _read_ci(runtime, watch)
    if read is not None:
        return {**read, **_fact(watch, read["result"], waited)}
    ceiling = ceiling_seconds()
    if waited >= ceiling:
        return _fact(
            watch,
            "timeout",
            waited,
            reason=(
                f"no terminal CI result for the merge within {ceiling // 60} min "
                f"(last seen: {watch.get('last_state') or 'nothing'})"
            ),
            runs=list(watch.get("last_runs") or []),
        )
    return None


def _fact(
    watch: dict[str, Any], result: str, waited: int, *, reason: str = "", runs: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "result": result,
        "card": str(watch.get("ref") or ""),
        "base": str(watch.get("base") or ""),
        "merge_sha": str(watch.get("merge_sha") or ""),
        "waited_seconds": waited,
    }
    if reason:
        fact["reason"] = safe_one_line(reason)
    if runs is not None:
        fact["runs"] = runs
    return fact


def _repo_dir(runtime: Any, project: str) -> Path | None:
    try:
        return Path(str(runtime.catalog.binding(project)["repo"])).expanduser()
    except (KeyError, TypeError, ValueError, AttributeError, HostError):
        return None


def _read_ci(runtime: Any, watch: dict[str, Any]) -> dict[str, Any] | None:
    """A terminal green/red reading of the base CI for the merge commit, or None (pending).

    Every question goes through the gate's backend call; a transport or backend failure is
    remembered on the watch as its last state and is pending, never an answer.
    """
    host = runtime.host
    project = str(watch.get("project") or "")
    repo_dir = _repo_dir(runtime, project)
    try:
        if not watch.get("merge_sha"):
            sha = pr_merge_commit(host._run, str(watch.get("branch") or ""), repo_dir or Path("."))
            if not sha:
                watch["last_state"] = "the merge commit is not readable yet"
                return None
            watch["merge_sha"] = sha
        sha = str(watch["merge_sha"])
        if not watch.get("repo"):
            if repo_dir is None:
                watch["last_state"] = "the project checkout is not registered"
                return None
            watch["repo"] = _name_with_owner(host, str(repo_dir))
        repo = str(watch["repo"])
        required = _required_checks(host, {"project": project})
        items = _commit_checks(host, repo, sha)
    except (HostError, OSError, ValueError, TypeError, AttributeError) as exc:
        watch["last_state"] = safe_one_line(f"CI unreadable: {exc}", limit=300)
        return None
    rollup, _first = _rollup(items, required)
    selected = _selected_checks(items, required)
    runs = _runs(repo, selected)
    watch["last_runs"] = runs
    if rollup == "SUCCESS":
        watch["last_state"] = "success"
        return {"result": "green", "runs": runs}
    if rollup != "FAILURE":
        watch["last_state"] = f"{rollup.lower()} ({len(selected)} check(s))"
        return None
    failed = [item for item in selected if _check_result(item) == "fail"]
    classification, reasons = _classify(host, repo, failed)
    return {
        "result": "red",
        "runs": runs,
        "failed_checks": sorted({safe_one_line(_check_name(item)) or "?" for item in failed}),
        "classification": classification,
        **({"classification_reason": ", ".join(reasons)} if reasons else {}),
    }


def _commit_checks(host: Any, repo: str, sha: str) -> list[dict[str, Any]]:
    """Check-runs and commit statuses for exactly `sha`, with every hostile shape made pending.

    An entry that names another commit is dropped; a completed check-run without a conclusion is
    read as still running; a statuses answer for another commit is ignored whole.
    """
    items: list[dict[str, Any]] = []
    runs = _gh_api(host, f"repos/{repo}/commits/{sha}/check-runs", jq=".check_runs")
    if isinstance(runs, list):
        for item in runs:
            if not isinstance(item, dict):
                continue
            head = item.get("head_sha")
            if head is not None and str(head) != sha:
                continue
            if str(item.get("status") or "").upper() == "COMPLETED" and not str(item.get("conclusion") or ""):
                item = {**item, "status": "in_progress"}
            items.append(item)
    combined = _gh_api(host, f"repos/{repo}/commits/{sha}/status", jq=".")
    if isinstance(combined, dict) and str(combined.get("sha") or "") == sha:
        statuses = combined.get("statuses")
        if isinstance(statuses, list):
            items.extend(item for item in statuses if isinstance(item, dict))
    return items


def _runs(repo: str, items: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: list[str] = []
    for item in items:
        run_id = _actions_run_id(item)
        if run_id and run_id not in seen:
            seen.append(run_id)
    return [{"id": run_id, "url": f"https://github.com/{repo}/actions/runs/{run_id}"} for run_id in seen]


def _classify(host: Any, repo: str, failed: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """The gate's classification of a red: `infrastructure` only when every failed run read is.

    One failed job per run is read (`gate._failed_log`, `gate._classify_failed_step`), at most
    `_CLASSIFIED_RUNS` runs. A log that cannot be read classifies as product, as it does in the gate.
    """
    by_run: dict[str, dict[str, Any]] = {}
    for item in failed:
        by_run.setdefault(_actions_run_id(item), item)
    classes: list[str] = []
    reasons: list[str] = []
    for item in list(by_run.values())[:_CLASSIFIED_RUNS]:
        fragment = _failed_log(host, repo, item)
        classes.append(fragment.failure_class)
        if fragment.failure_reason:
            reasons.append(fragment.failure_reason)
    if classes and all(value == "infrastructure" for value in classes):
        return "infrastructure", reasons
    return "product", reasons


def render_fact(fact: dict[str, Any]) -> str:
    """One post-merge CI fact in words, for the card, the sprint comment and the observer wake."""
    result = str(fact.get("result") or "")
    card = str(fact.get("card") or "")
    base = str(fact.get("base") or "")
    sha = str(fact.get("merge_sha") or "")
    where = f"`{base}` @ `{sha[:12]}`" if sha else f"`{base}` (merge commit unknown)"
    runs = fact.get("runs") if isinstance(fact.get("runs"), list) else []
    run_text = ", ".join(
        f"{run.get('id')} ({run.get('url')})" for run in runs if isinstance(run, dict) and run.get("id")
    )
    lines = [f"Post-merge CI {result.upper()} for {card}: merge {where}."]
    if result == "green":
        lines.append("Every selected check on the merge commit finished successfully.")
    elif result == "red":
        lines.append(
            "The release merged, but the base's CI failed on the merge commit: this is not a plain Done."
        )
        failed = fact.get("failed_checks") if isinstance(fact.get("failed_checks"), list) else []
        lines.append("- failed checks: " + (", ".join(str(name) for name in failed) or "(unnamed)"))
        classification = str(fact.get("classification") or "product")
        detail = str(fact.get("classification_reason") or "")
        lines.append(f"- classification: {classification}" + (f" ({detail})" if detail else ""))
    elif result == "absent":
        lines.append("No CI run validates the base after this merge.")
    elif result == "timeout":
        lines.append("The base's CI gave no terminal result for the merge commit within the ceiling.")
    lines.append("- run(s): " + (run_text or "none"))
    if fact.get("reason"):
        lines.append(f"- reason: {fact['reason']}")
    return "\n".join(lines)


def _request_id(watch: dict[str, Any], kind: str) -> str:
    return f"post-merge-ci-{kind}-{watch.get('id') or ''}-{watch.get('ref') or ''}"


def _sprint_writer(runtime: Any) -> Any:
    writer = getattr(runtime, "sprint_writer", None)
    if writer is not None:
        return writer
    from secretary.sprints import SprintWriter, budget_thresholds

    instance = getattr(runtime.catalog, "instance", {})
    return SprintWriter(
        runtime.reader.client,
        data_dir=Path(getattr(runtime, "data_dir", None) or Path(runtime.audit.board_dir).parent),
        thresholds=budget_thresholds(instance if isinstance(instance, dict) else None),
    )


def publish(runtime: Any, watch: dict[str, Any]) -> None:
    """Write the recorded result: the card event, then the sprint comment. Both are idempotent."""
    fact = dict(watch["result"])
    body = render_fact(fact)
    runtime.writer.post_merge_ci(
        actor=runtime.owner,
        reference=str(watch["ref"]),
        body=body,
        fact=fact,
        request_id=_request_id(watch, "card"),
    )
    sprint = str(watch.get("sprint") or "")
    if sprint:
        _sprint_writer(runtime).comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=sprint,
            body=body,
            request_id=_request_id(watch, "sprint"),
        )


def wake_facts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The post-merge CI facts among a batch of observer wake events, in order."""
    facts = []
    for event in events:
        fact = post_merge_ci_fact(event)
        if fact is not None:
            facts.append(fact)
    return facts


__all__ = [
    "CEILING_ENV",
    "DEFAULT_CEILING_SECONDS",
    "POST_MERGE_CI_KEY",
    "WATCHES_KEY",
    "open_watch",
    "publish",
    "reconcile_post_merge_watches",
    "release_merge_marker",
    "render_fact",
    "resolve",
    "wake_facts",
]
