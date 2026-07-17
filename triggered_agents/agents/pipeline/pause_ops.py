"""Worker/reviewer head transitions for hard pause and resume — the one place that decides,
for a card whose live terminal was stopped by a freeze, whether it should move by an already
posted report, relaunch a fresh head, or stay parked for a lazy rework return.

Split out of dispatcher.py (triggered-agents-399) for two reasons the third-review verdict on
PR #85 made concrete:

  * one source of truth. The "move an In-progress card by its worker's [report:done]/
    [report:blocked]" step used to be inlined in dispatcher._advance and NOT applied on the resume
    path, so resume() would relaunch a fresh head into a card that had already reported during the
    freeze — a new terminal under a card in Validate/Blocked that nobody owned (review blocker B1).
    apply_report_transition() now holds that step and both _advance and resume_hard call it before
    ever touching a head, so a report that landed mid-freeze always wins over a relaunch.

  * dispatcher.py was pushed over the 1000-line review ceiling by the pause/resume/park logic.
    Moving the stop loop, the resume loop and the relaunch helper here keeps that state machine in
    one cohesive module instead of sprawled across _advance, pause() and resume().

Dependency injection mirrors validate.run: the caller (dispatcher) still owns cards.json I/O and
passes in refresh_worker_task, so this module imports only the low-level layers (ops/worker/
validate/model/naming) and never imports dispatcher back.
"""
from __future__ import annotations

import os
import time

from . import model, naming, ops, validate, worker
from .state import STATE


def report_verdict(reference: str, baseline: int) -> str | None:
    """'done'/'blocked'/None from the worker's comments past `baseline` (the count at launch).
    The last report wins if a worker somehow posted more than one."""
    comments = ops.show_card(reference)["comments"]
    verdict = None
    for c in comments[baseline:]:
        text = c.get("text", "")
        if f"[{model.MARKER_REPORT_DONE}]" in text:
            verdict = "done"
        elif f"[{model.MARKER_REPORT_BLOCKED}]" in text:
            verdict = "blocked"
    return verdict


def apply_report_transition(ref: str, card: dict, rec: dict, records: dict) -> str | None:
    """Report-first move for a tracked In-progress card. Returns 'done'/'blocked' when a report was
    pending (and performs the move + bookkeeping), else None so the caller falls through to its own
    head handling (watchdog in _advance, relaunch/park in resume_hard).

    'done' keeps the record: the worker session lives on for CI rework and the baseline is advanced
    past this report so a CI-red return to In progress doesn't re-read the same done comment and
    bounce straight back. Every (re)entry into Validate is a fresh code state, so the stand budget
    and any review state are reset here. 'blocked' drops the record; the card is terminal until a
    human or steward moves it."""
    verdict = report_verdict(ref, int(rec.get("comment_baseline", 0)))
    if verdict == "done":
        ops.move_card("dispatcher", ref, "Validate")
        validate.clear_review(rec)
        rec["comment_baseline"] = len(ops.show_card(ref)["comments"])
        rec["stand_fails"] = 0
        rec.pop("ci_pending_since", None)
        STATE.log_run("advance", reference=ref, to="Validate", reason="report:done")
        return "done"
    if verdict == "blocked":
        ops.move_card("dispatcher", ref, "Blocked")
        STATE.log_run("advance", reference=ref, to="Blocked", reason="report:blocked")
        records.pop(ref, None)
        return "blocked"
    return None


def relaunch_worker_after_resume(ref: str, card: dict, rec: dict, refresh_worker_task,
                                 reason: str) -> str:
    """Start a fresh worker terminal in the existing workspace after a freeze stopped the old one.
    TASK.md is rewritten from the card's current journal first, so a verdict or operator comment
    posted during the pause is part of the prompt the relaunched worker reads.

    Only the launch itself may raise "relaunch failed" out of here. The moment worker.launch_worker
    returns a handle, that terminal is live, so the handle and reset state are recorded before any
    board write, and the courtesy card comment plus the success log are best-effort — a Kanboard
    hiccup there must never propagate as a failure, or the caller's failure branch would wipe the
    handle of an already-running head and let the next tick's dead-handle watchdog tear down a
    workspace that has a fresh worker in it (PR #85 review B1)."""
    workspace = rec.get("workspace")
    if not workspace:
        raise RuntimeError("worker workspace is missing")
    refresh_worker_task(card, rec)
    title = rec.get("title") or naming.worker_title(naming.card_id(ref), card.get("title") or ref)
    head = rec.get("head") or card.get("head")
    handle = worker.launch_worker(workspace, head, rec.get("worker", ""), title)
    if not handle:
        raise RuntimeError("worker launch returned no handle")
    rec["handle"] = handle
    rec["title"] = title
    rec["head"] = head
    rec["terminal_kind"] = worker.terminal_kind(head)
    rec["last_activity"] = time.time()
    rec.pop("parked_worker", None)
    rec.pop("park_notice_logged", None)
    rec.pop("unpark_fails", None)
    try:
        ops.add_comment(
            "dispatcher", ref,
            f"терминал остановлен паузой, перезапущен. Воркспейс {workspace}, причина: {reason}.")
        STATE.log_run("relaunch-after-resume", reference=ref, result="relaunched",
                      reason=reason, workspace=workspace, handle=handle)
    except Exception as e:  # noqa: BLE001 — head is already live and recorded; a failed courtesy
        # comment/log must not roll the relaunch back into a lost-handle watchdog teardown.
        STATE.log_run("relaunch-after-resume", reference=ref, result="relaunched-comment-failed",
                      reason=reason, workspace=workspace, handle=handle, level="warn",
                      error=worker.scrub_secrets(str(e)))
    return handle


def plan_hard_pause(records: dict, by_ref: dict, excluded_paths: set[str]) -> tuple[
        list[str], list[str], list[str]]:
    """Stop the live heads a hard pause must freeze. Returns (stopped_worker, stopped_reviewer,
    excluded_worker). stop_terminals only — never a teardown, so the branch/uncommitted work stays
    exactly as the head left it. An In-progress workspace in `excluded_paths` (the narrow
    initiator/operator exclude, e.g. a backup-create worker) keeps running instead of being
    stopped. A Validate worker is parked rather than treated as an active writer: the independent
    reviewer or CI polling owns the card while it sits in Validate."""
    stopped_worker: list[str] = []
    stopped_reviewer: list[str] = []
    excluded_worker: list[str] = []
    for ref, rec in records.items():
        card = by_ref.get(ref)
        if card is None:
            continue
        workspace = rec.get("workspace")
        if card["column"] == model.IN_PROGRESS and workspace:
            if os.path.abspath(workspace) in excluded_paths:
                excluded_worker.append(ref)
                continue
            worker.stop_terminals(workspace)
            stopped_worker.append(ref)
        elif card["column"] == "Validate" and workspace:
            worker.stop_terminals(workspace)
            stopped_worker.append(ref)
        if card["column"] == "Validate" and rec.get("review_ws"):
            worker.stop_terminals(rec["review_ws"])
            stopped_reviewer.append(ref)
    return stopped_worker, stopped_reviewer, excluded_worker


def resume_hard(state: dict, records: dict, by_ref: dict, refresh_worker_task) -> dict:
    """Undo a hard pause's head stops. Mutates `records` in place and returns the log buckets the
    caller writes to runs.jsonl. Every stopped card follows report-first: a card that reported
    while frozen is moved by that report (moved bucket) and never gets a fresh head. Only a still
    In-progress card with no pending report is relaunched; a Validate worker is left parked for a
    lazy CI-red/review-red rework return; an excluded worker just has its watchdog clock reset.

    Buckets:
      relaunched — worker/reviewer heads started fresh in their existing workspaces
      parked     — Validate workers kept for rework return (handle cleared, parked_worker set)
      excluded   — In-progress workers left running by an explicit exclude (clock reset only)
      moved      — cards advanced by a report that landed during the freeze (no head touched)
      skipped    — cards that no longer want a head (moved on, record gone, relaunch failed)"""
    relaunched: list[str] = []
    parked: list[str] = []
    excluded: list[str] = []
    skipped: list[str] = []
    moved: list[str] = []

    for ref in state.get("excluded_worker") or []:
        rec = records.get(ref)
        card = by_ref.get(ref)
        if rec is None or card is None or card["column"] != model.IN_PROGRESS:
            skipped.append(f"{ref}:worker")
            continue
        rec["last_activity"] = time.time()
        excluded.append(f"{ref}:worker")

    for ref in state.get("stopped_worker") or []:
        rec = records.get(ref)
        card = by_ref.get(ref)
        if rec is None or card is None or not rec.get("workspace"):
            skipped.append(f"{ref}:worker")
            continue
        if card["column"] == model.IN_PROGRESS:
            # B1: a [report:done]/[report:blocked] posted during the freeze wins over a relaunch.
            # Without this, resume() started a fresh head and only the next tick read the report,
            # leaving a new terminal under a card already on its way to Validate or Blocked.
            verdict = apply_report_transition(ref, card, rec, records)
            if verdict == "done":
                # Finished before the freeze lifted: the card is now in Validate, so treat the
                # stopped worker exactly like a parked Validate worker — no fresh head until a
                # CI-red/review-red return hands it back for rework.
                rec["handle"] = ""
                rec["parked_worker"] = True
                rec.pop("ci_pending_since", None)
                moved.append(f"{ref}:worker:done")
                continue
            if verdict == "blocked":
                # Moved to Blocked, record already dropped by apply_report_transition. The terminal
                # stays stopped; nothing to relaunch into a terminal column.
                moved.append(f"{ref}:worker:blocked")
                continue
            try:
                relaunch_worker_after_resume(ref, card, rec, refresh_worker_task, "pipeline resume")
            except Exception as e:  # noqa: BLE001, one bad relaunch must not lose the rest
                rec["handle"] = ""
                rec.pop("parked_worker", None)
                rec["last_activity"] = time.time()
                skipped.append(f"{ref}:worker")
                ops.add_comment(
                    "dispatcher", ref,
                    "терминал остановлен паузой, перезапуск при resume не удался. "
                    "Карточка остаётся In progress под обычным watchdog; следующий tick "
                    f"увидит пустой handle и применит retry policy. Ошибка: "
                    f"{worker.scrub_secrets(str(e))}.")
                STATE.log_run("relaunch-after-resume", reference=ref, result="failed",
                              reason="pipeline resume", level="warn",
                              error=worker.scrub_secrets(str(e)))
                continue
            relaunched.append(f"{ref}:worker")
        elif card["column"] == "Validate":
            # A parked Validate worker (kept for CI rework) must NOT be actively relaunched here: an
            # independent reviewer may be reviewing this exact branch right now, and a fresh worker
            # head reading TASK.md from scratch would start a new turn and risk pushing commits
            # under it — branch drift under review (triggered-agents-281 review, blocker B1).
            # validate.py relaunches it lazily only when a CI-red/review-red return actually hands
            # the card back for rework. ci_pending_since is dropped so a card still on a
            # non-terminal CI rollup gets a fresh stall window instead of one that elapsed while
            # paused.
            rec["handle"] = ""
            rec["parked_worker"] = True
            rec.pop("ci_pending_since", None)
            parked.append(f"{ref}:worker")
        else:
            skipped.append(f"{ref}:worker")

    for ref in state.get("stopped_reviewer") or []:
        rec = records.get(ref)
        card = by_ref.get(ref)
        if (rec is None or card is None or card["column"] != "Validate"
                or not rec.get("review_ws")):
            skipped.append(f"{ref}:reviewer")
            continue
        try:
            rec["review_handle"] = worker.relaunch_reviewer(
                rec["review_ws"], rec.get("worker", ref), rec.get("review_title", ref),
                rec.get("review_head"))
            rec["review_terminal_kind"] = worker.reviewer_terminal_kind(rec.get("review_head"))
        except Exception as e:  # noqa: BLE001 — one bad relaunch must not lose the rest
            skipped.append(f"{ref}:reviewer")
            STATE.log_run("resume", reference=ref, result="relaunch-failed",
                          level="warn", error=worker.scrub_secrets(str(e)))
            continue
        rec["review_activity"] = time.time()
        rec.pop("ci_pending_since", None)
        relaunched.append(f"{ref}:reviewer")

    return {"relaunched": relaunched, "parked": parked, "excluded": excluded,
            "skipped": skipped, "moved": moved}
