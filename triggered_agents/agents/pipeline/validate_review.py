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
        label = pr if pr else f"branch `{contrib[0]}` @ `{contrib[1]}`"
        ops.add_comment(
            "dispatcher", ref,
            f"The PO disabled the LLM review for this card (`review_head=none`). The lower "
            f"validation layers are green; layer 3 was skipped on {label}.",
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
                        f"PR {pr} was opened against `{actual_base}` while base `{expected_base}` "
                        f"was expected; the automatic merge was stopped and the card moved to "
                        f"Blocked.")
        ops.move_card("dispatcher", ref, "Blocked")
        records.pop(ref, None)
        STATE.log_run("review", reference=ref, to="Blocked", reason="base-mismatch", pr=pr,
                      expected=expected_base, actual=actual_base)
        return True
    rec["automerge_done"] = True
    result = worker.merge_pr(pr)
    if result["ok"]:
        if review_skipped:
            layers = ("CI, stand, LLM review disabled by the PO" if is_stand
                      else "CI, LLM review disabled by the PO")
        else:
            layers = "CI, stand, review" if is_stand else "CI, review"
        ops.add_comment("dispatcher", ref,
                        f"All validation layers are green ({layers}); merging {pr} automatically.",
                        marker=model.MARKER_AUTOMERGE)
        log_result = "no-review-automerge" if review_skipped else "green-automerge"
        STATE.log_run("review", reference=ref, result=log_result, pr=pr)
        return True
    scrubbed = worker.scrub_secrets(result.get("error") or "(no details)")
    ops.add_comment("dispatcher", ref,
                    f"The automatic merge of {pr} failed: {scrubbed}. Card moved to Blocked; it "
                    f"needs a manual check and a manual merge.")
    ops.move_card("dispatcher", ref, "Blocked")
    records.pop(ref, None)
    STATE.log_run("review", reference=ref, to="Blocked", reason="automerge-fail", pr=pr,
                  error=scrubbed)
    return True
