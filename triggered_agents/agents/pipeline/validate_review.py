"""Validate layer-3 terminal paths shared by reviewer-green and PO no-review."""
from __future__ import annotations

import os

from . import model, ops, worker
from .state import STATE


def automerge_enabled() -> bool:
    """Kill switch for dispatcher squash-merge on a green layer-3 outcome."""
    return os.environ.get("TA_AUTOMERGE", "").strip().lower() not in ("off", "0", "false")


def review_skipped_by_po(ref: str, pr: str | None, card: dict, is_stand: bool, rec: dict,
                         records: dict, contrib: tuple[str, str] | None = None) -> bool:
    """PO set review_head=none: lower layers are already green, so skip only layer 3."""
    changed = False
    if not rec.get("no_review_logged"):
        label = pr if pr else f"ветке `{contrib[0]}` @ `{contrib[1]}`"
        ops.add_comment(
            "dispatcher", ref,
            f"PO отключил LLM-review для этой карточки (`review_head=none`). Нижние слои "
            f"валидации зелёные; слой 3 пропущен по {label}.",
            marker=model.MARKER_REVIEW_SKIPPED)
        rec["no_review_logged"] = True
        STATE.log_run("review", reference=ref, result="skipped-by-po", pr=pr,
                      review_head=model.NO_REVIEW_HEAD)
        changed = True
    return review_green(ref, pr, card, is_stand, rec, records, contrib,
                        review_skipped=True) or changed


def _review_green_contrib(ref: str, rec: dict, records: dict,
                          review_skipped: bool = False) -> bool:
    """Contrib card, green verdict: no PR remains to wait for in this pipeline."""
    ws = rec.get("workspace")
    ops.move_card("dispatcher", ref, "Done")
    records.pop(ref, None)
    if ws:
        worker.teardown(ws)
    reason = "contrib-no-review" if review_skipped else "contrib-green"
    STATE.log_run("review", reference=ref, to="Done", reason=reason)
    return True


def review_green(ref: str, pr: str | None, card: dict, is_stand: bool, rec: dict, records: dict,
                 contrib: tuple[str, str] | None = None,
                 review_skipped: bool = False) -> bool:
    """Green layer-3 outcome: teardown reviewer worktree, then Done or automerge."""
    changed = False
    if rec.get("review_ws"):
        worker.teardown(rec["review_ws"])
        rec["review_ws"] = ""
        changed = True
    if contrib is not None:
        return _review_green_contrib(ref, rec, records, review_skipped) or changed
    if not automerge_enabled():
        if rec.get("review_green_logged"):
            return changed
        rec["review_green_logged"] = True
        result = "no-review-green" if review_skipped else "green"
        STATE.log_run("review", reference=ref, result=result)
        return True
    if rec.get("automerge_done"):
        return changed
    expected_base = worker.resolve_base_branch(card.get("project") or "", card.get("base_branch") or "")
    actual_base = worker.pr_base_branch(pr)
    if actual_base is None:
        return changed
    if actual_base != expected_base:
        ops.add_comment("dispatcher", ref,
                        f"PR {pr} открыт против `{actual_base}`, ожидалась база `{expected_base}` — "
                        f"автомерж остановлен, карточка в Blocked.")
        ops.move_card("dispatcher", ref, "Blocked")
        records.pop(ref, None)
        STATE.log_run("review", reference=ref, to="Blocked", reason="base-mismatch", pr=pr,
                      expected=expected_base, actual=actual_base)
        return True
    rec["automerge_done"] = True
    result = worker.merge_pr(pr)
    if result["ok"]:
        if review_skipped:
            layers = "CI, стенд, LLM-review отключён PO" if is_stand else "CI, LLM-review отключён PO"
        else:
            layers = "CI, стенд, ревью" if is_stand else "CI, ревью"
        ops.add_comment("dispatcher", ref,
                        f"Все слои валидации зелёные ({layers}) — автомерж {pr}.",
                        marker=model.MARKER_AUTOMERGE)
        log_result = "no-review-automerge" if review_skipped else "green-automerge"
        STATE.log_run("review", reference=ref, result=log_result, pr=pr)
        return True
    scrubbed = worker.scrub_secrets(result.get("error") or "(без деталей)")
    ops.add_comment("dispatcher", ref,
                    f"Автомерж {pr} не удался: {scrubbed}. Карточка в Blocked, нужна ручная "
                    f"проверка и мерж руками.")
    ops.move_card("dispatcher", ref, "Blocked")
    records.pop(ref, None)
    STATE.log_run("review", reference=ref, to="Blocked", reason="automerge-fail", pr=pr,
                  error=scrubbed)
    return True
