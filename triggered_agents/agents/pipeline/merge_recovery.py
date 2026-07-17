"""Validate base-freshness recovery (triggered-agents-442).

When a Validate PR's head diverges from its base — GitHub reports mergeable=CONFLICTING or
mergeStateStatus=BEHIND (normalized by model.merge_status, carried on poll_pr's `mergeable`) — the
branch is recovered here instead of hanging under the CI watchdog as «checks не появились» (the
codegen_orchestrator-440 incident: a behind/conflicting PR never starts its pull_request workflow,
so rollup stays NONE forever). A branch that is only BEHIND (base moved ahead, no textual conflict)
is auto-updated: an ordinary `git merge origin/<base>` in the worker's existing workspace, a merge
commit, a plain push — never a rebase, never a force-push. The card stays in Validate with every
stale layer clock/marker reset, so layer 1 re-runs for the new head SHA and no old green marker or
reviewer verdict is reused. A real text conflict goes back to the same worker for a semantic
resolution. Both are budget-capped per base SHA so one divergence never loops forever.

Split out of validate.py the same way validate_review.py / review_spawn.py are: this is a leaf
module (imports only the low-level layers), and validate.run threads in the rework helpers it needs
(clear_review, the worker-nudge, the relaunch-failure escalation) as callbacks rather than importing
validate back — mirroring how review_spawn.block_inject_delivery already takes clear_review. The git
merge itself is the host half and reuses worker._git (the same bounded git runner every other
host-side git step in the pipeline goes through), so the merge op and its orchestration stay in one
cohesive module, the way stand.py holds the stand's host work.
"""
from __future__ import annotations

import os
import time

from . import model, naming, ops, worker
from .state import STATE

# How many base-freshness recovery actions (an auto-merge of a moved base, or a return to the worker
# for a text conflict) a card may take against ONE base SHA before Blocked. Keyed on the base SHA
# (rec["merge_recovery"]): a base that keeps moving gets a fresh budget each time, but the same
# divergence never loops forever.
MERGE_RECOVERY_ATTEMPTS = int(os.environ.get("TA_MERGE_RECOVERY_ATTEMPTS", "3"))


def merge_base_into_branch(workspace: str, base_branch: str, branch: str) -> dict:
    """Merge origin/<base_branch> into <branch> inside the existing worker workspace, creating a
    merge commit (never a rebase) and pushing it. Returns a dict whose "result" is one of:
        "updated"      merge commit created and pushed; "head" is the new sha
        "already"      branch already contained the base; origin re-synced, "head" unchanged
        "conflict"     text conflict; the merge was aborted, "conflict_files" lists the files
        "dirty"        worktree has uncommitted worker code — refused untouched, nothing merged
        "merge-failed" git merge failed with no conflicted files (some other merge error)
        "fetch-failed" / "push-failed" / "error"   the named git step failed; "reason" carries why
    Every failure path leaves the worktree exactly as found: a conflict is `git merge --abort`ed and
    a dirty tree is never touched, so a half-done or racing worker never loses uncommitted code
    (AC6). The worktree is checked/moved onto `branch` before the merge so a worker that left it on
    main or detached never has the base merged into the wrong checkout or a stale PR branch pushed
    (B2). Distinct results so the journal can tell a conflict from a fetch/merge/push failure. The
    caller scrubs "reason"/"conflict_files" before any board comment."""
    git = worker._git
    try:
        dirty = git(workspace, ["status", "--porcelain"])
        if dirty.returncode != 0:
            return {"result": "error", "reason": (dirty.stderr or dirty.stdout).strip() or "git status failed"}
        if dirty.stdout.strip():
            return {"result": "dirty", "reason": "worktree has uncommitted changes"}
        cur = git(workspace, ["rev-parse", "--abbrev-ref", "HEAD"])
        if cur.returncode != 0:
            return {"result": "error", "reason": (cur.stderr or cur.stdout).strip() or "rev-parse failed"}
        if cur.stdout.strip() != branch:   # worker left it on main/another branch/detached HEAD
            checkout = git(workspace, ["checkout", branch])
            if checkout.returncode != 0:
                return {"result": "error",
                        "reason": f"worktree not on {branch} and checkout failed: "
                                  f"{(checkout.stderr or checkout.stdout).strip()}"}
        before = git(workspace, ["rev-parse", "HEAD"])
        if before.returncode != 0:
            return {"result": "error", "reason": (before.stderr or before.stdout).strip() or "not a git worktree"}
        fetch = git(workspace, ["fetch", "origin", base_branch], timeout=worker.GH_TIMEOUT_S)
        if fetch.returncode != 0:
            return {"result": "fetch-failed", "reason": (fetch.stderr or fetch.stdout).strip()}
        merge = git(workspace, ["merge", "--no-edit", "FETCH_HEAD"])
        if merge.returncode != 0:
            files = git(workspace, ["diff", "--name-only", "--diff-filter=U"])
            conflict_files = [f for f in files.stdout.splitlines() if f.strip()] if files.returncode == 0 else []
            git(workspace, ["merge", "--abort"])   # restore the tree; never leave conflict markers
            if conflict_files:
                return {"result": "conflict", "conflict_files": conflict_files}
            return {"result": "merge-failed", "reason": (merge.stderr or merge.stdout).strip() or "merge failed"}
        after = git(workspace, ["rev-parse", "HEAD"])
        head = after.stdout.strip() if after.returncode == 0 else None
        push = git(workspace, ["push", "origin", branch], timeout=worker.GH_TIMEOUT_S)
        if push.returncode != 0:
            return {"result": "push-failed", "reason": (push.stderr or push.stdout).strip(), "head": head}
        return {"result": "updated" if head != before.stdout.strip() else "already", "head": head}
    except worker.WorkspaceError as e:   # a bounded git step timed out (_git raises this on timeout)
        return {"result": "error", "reason": str(e)}


def recover(ref: str, pr: str, card: dict, rec: dict | None, records: dict, status: dict,
            save_cards, refresh_worker_task, *, clear_review, notify_rework, block_rework) -> bool:
    """Recover a Validate PR whose head diverged from its base. Drives the actual git merge in the
    worker workspace (merge_base_into_branch) and routes on the result: a clean update stays in
    Validate (_update), a conflict/dirty tree returns to the worker (_conflict), a fetch/merge/push
    failure is logged distinctly and bounded. Idempotent on the base SHA: the attempt count and the
    head we produced live in rec["merge_recovery"], so a push GitHub hasn't recomputed yet does not
    re-merge, and the same base never exceeds MERGE_RECOVERY_ATTEMPTS before Blocked. The rework
    helpers (clear_review, notify_rework, block_rework) are passed in by validate.run so this module
    never imports validate back."""
    merge_state = status.get("mergeable")
    base_sha = status.get("base_sha") or ""
    head_sha = status.get("head_sha") or ""
    if rec is None:
        # Untracked Validate card (manual move / lost cards.json): no worker workspace to merge in
        # or relaunch, so recovery can't run — a bare warn, same limited handling every other
        # untracked-card path takes (see validate._review_gate / _validate_stall).
        STATE.log_run("validate", reference=ref, result="merge-recovery-untracked", level="warn",
                      pr=pr, merge_state=merge_state)
        return False
    workspace = rec.get("workspace")
    if not workspace:
        STATE.log_run("validate", reference=ref, result="merge-recovery-no-workspace", level="warn",
                      pr=pr, merge_state=merge_state)
        return False

    state = rec.get("merge_recovery") or {}
    same_base = state.get("base") == base_sha
    if same_base and state.get("head") and state.get("head") == head_sha:
        # Our own merge for this base already landed on the PR head; GitHub just hasn't recomputed
        # mergeStateStatus yet. Wait rather than merge again (one base SHA, one merge — AC5).
        STATE.log_run("validate", reference=ref, result="merge-recovery-settled", pr=pr,
                      base_sha=base_sha, head_sha=head_sha)
        return False
    # The base to merge is the PR's ACTUAL base ref (poll_pr's base_branch / GitHub baseRefName),
    # never a card-derived guess (B1): a card whose worker opened the PR against the wrong base
    # (e.g. main instead of sprint/NNN) must not have the card's expected base merged into a branch
    # that will land somewhere else — recovery keeps the PR consistent with the branch it actually
    # targets, and the card/PR base mismatch is still caught by validate_review.review_green's own
    # base guard before any merge. Missing base_branch (gh gave us mergeability but no base ref) is
    # not something to guess at — wait a tick, like a gh blip.
    base = status.get("base_branch") or ""
    if not base:
        STATE.log_run("validate", reference=ref, result="merge-recovery-no-base", level="warn",
                      pr=pr, merge_state=merge_state)
        return False
    attempts = int(state.get("attempts", 0)) if same_base else 0
    if attempts >= MERGE_RECOVERY_ATTEMPTS:
        return _block(ref, pr, rec, records, base_sha, attempts, merge_state, clear_review)

    branch = naming.worker_branch(ref)
    result = merge_base_into_branch(workspace, base, branch)
    attempts += 1
    rec["merge_recovery"] = {"base": base_sha, "attempts": attempts}
    kind = result.get("result")
    if kind in ("updated", "already"):
        rec["merge_recovery"]["head"] = result.get("head")
        return _update(ref, pr, rec, base, branch, result, base_sha, clear_review)
    if kind in ("conflict", "dirty"):
        return _conflict(ref, pr, card, rec, records, save_cards, refresh_worker_task, result, base,
                         base_sha, head_sha, attempts, clear_review, notify_rework, block_rework)
    scrubbed = worker.scrub_secrets(result.get("reason") or "(без деталей)")
    STATE.log_run("validate", reference=ref, result=f"merge-{kind}", level="warn", pr=pr,
                  base_sha=base_sha, attempts=attempts, error=scrubbed)
    if attempts >= MERGE_RECOVERY_ATTEMPTS:
        return _block(ref, pr, rec, records, base_sha, attempts, merge_state, clear_review,
                      detail=f"{kind}: {scrubbed}")
    return True


def _update(ref: str, pr: str, rec: dict, base: str, branch: str, result: dict, base_sha: str,
            clear_review) -> bool:
    """A clean auto-merge of the moved base landed on the PR head and pushed. Stay in Validate but
    drop every stale layer-1/2/3 clock and reset the marker baseline so layer 1 re-runs for the NEW
    head SHA — an old green CI marker or reviewer verdict must never be reused, and the reviewer
    must not start before the new state's CI is green (AC7). Then wait for the new SHA's checks."""
    kind = result.get("result")
    new_head = result.get("head") or "?"
    clear_review(rec)                      # tear down any in-flight reviewer worktree + its baseline
    rec["stand_fails"] = 0
    rec.pop("ci_pending_since", None)
    if kind == "updated":
        body = (f"Ветка PR отставала от базы `{base}` без текстового конфликта — сделан обычный "
                f"merge базы в `{branch}` (merge-коммит, без rebase/force-push), новый head "
                f"`{new_head}`. Слои валидации сброшены, ждём CI нового состояния. PR: {pr}")
    else:
        body = (f"Ветка PR уже содержала базу `{base}`, origin синхронизирован (head `{new_head}`). "
                f"Слои валидации сброшены, ждём CI нового состояния. PR: {pr}")
    ops.add_comment("dispatcher", ref, body)
    rec["comment_baseline"] = len(ops.show_card(ref)["comments"])
    STATE.log_run("validate", reference=ref, result=f"merge-{kind}", pr=pr, base_sha=base_sha,
                  head_sha=new_head, base=base)
    return True


def _conflict(ref: str, pr: str, card: dict, rec: dict, records: dict, save_cards,
              refresh_worker_task, result: dict, base: str, base_sha: str, head_sha: str,
              attempts: int, clear_review, notify_rework, block_rework) -> bool:
    """A text conflict (or a worktree with uncommitted worker code) can't be merged mechanically —
    hand the card back to the SAME worker in its existing workspace to resolve it, exactly like a
    CI-red return, but with the conflicting files and base SHA spelled out and an instruction to
    merge/resolve/push/re-report. Workspace and feature branch are preserved (AC4). Capped on the
    base SHA (AC5): a conflict the worker can't resolve for one base ends in Blocked, not a
    ping-pong forever."""
    if attempts >= MERGE_RECOVERY_ATTEMPTS:
        return _block(ref, pr, rec, records, base_sha, attempts, "CONFLICTING", clear_review,
                      detail="conflict not resolved within budget")
    if result.get("result") == "dirty":
        detail = ("В воркспейсе воркера есть незакоммиченные изменения — автослияние базы отменено, "
                  "ничего не тронуто.")
        files_note = ""
    else:
        files = result.get("conflict_files") or []
        shown = "\n".join(f"- `{worker.scrub_secrets(f)}`" for f in files) if files else \
            "(git не назвал файлы)"
        detail = f"Ветка PR конфликтует с базой `{base}` при обычном merge."
        files_note = f"\nКонфликтные файлы:\n{shown}"
    ops.add_comment(
        "dispatcher", ref,
        f"Требуется ручное разрешение расхождения с базой. {detail}{files_note}\n"
        f"База: `{base}` (sha `{base_sha or '?'}`), head PR: `{head_sha or '?'}`. Карточка "
        f"возвращена в In progress: в своей ветке сделай "
        f"`git fetch origin {base} && git merge origin/{base}`, разреши конфликт, прогони "
        f"проверки, запушь merge-коммит (без rebase/force-push) и снова report done. PR: {pr}")
    ops.move_card("dispatcher", ref, model.IN_PROGRESS)
    clear_review(rec)
    rec["comment_baseline"] = len(ops.show_card(ref)["comments"])
    rec["last_activity"] = time.time()
    rec["stand_fails"] = 0
    rec.pop("ci_pending_since", None)
    try:
        notify_rework(
            ref, card, rec, records, save_cards, refresh_worker_task,
            f"Расхождение ветки с базой по {pr}: нужно вручную смержить базу и разрешить конфликт. "
            f"Разбор в комментарии карточки, почини, запушь и снова report done.",
            "merge-conflict")
    except Exception as e:  # noqa: BLE001, do not leave In progress with a dead handle
        block_rework(ref, rec, records, "merge-conflict", e)
        return True
    STATE.log_run("validate", reference=ref, to=model.IN_PROGRESS, reason="merge-conflict", pr=pr,
                  base_sha=base_sha, attempts=attempts, files=result.get("conflict_files") or [])
    return True


def _block(ref: str, pr: str, rec: dict, records: dict, base_sha: str, attempts: int,
           merge_state: str | None, clear_review, detail: str = "") -> bool:
    """Base-freshness recovery budget spent on one base SHA — Blocked до vladmesh, worker workspace
    left alive for a human to inspect (AC5)."""
    ws = rec.get("workspace") or "(неизвестен)"
    clear_review(rec)
    tail = f" ({detail})" if detail else ""
    ops.add_comment("dispatcher", ref,
                    f"Recovery расхождения ветки с базой не сошёлся за {attempts} попыток на одном "
                    f"base sha `{base_sha or '?'}`{tail}. Карточка в Blocked, воркспейс {ws} "
                    f"оставлен для разбора. PR: {pr}")
    ops.move_card("dispatcher", ref, "Blocked")
    records.pop(ref, None)
    STATE.log_run("validate", reference=ref, to="Blocked", reason="merge-recovery-budget",
                  base_sha=base_sha, attempts=attempts, merge_state=merge_state)
    return True
