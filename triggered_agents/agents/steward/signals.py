"""Deterministic anomaly signals — what the steward's precheck gate and `/steward` skill both
read before anything judges.

Five signal kinds (2026-07-04 design grill, memory id 83 — "стюард присмотр пайплайн дизайн"):
new Blocked card, warn/error/head-health-flip line in the pipeline's own runs.jsonl since the
steward's watermark, a card sitting in an active column past STALE_HOURS, a resource health
flip, a worker/reviewer workspace on disk with no in-flight card record. Any one is enough for
precheck to spawn the head; finding none costs nothing (a few Kanboard reads and a couple of
local file stats, no LLM).

Every signal dedupes against a persisted "already notified" watermark (state/steward/
watermark.json), the same shape as curator/retro's watermark but keyed by anomaly kind rather
than by source: a condition that hasn't changed since the last run (a card still sitting in
Blocked, a resource still red) does not re-spawn the head every hour — the steward already
looked, and re-litigating an unresolved anomaly on an unchanged state is exactly the kind of
hourly LLM-cost sweep this agent is not meant to be. `scan` is read-only; `advance` (cli.py, only
after the skill has actually looked at the batch) folds the scanned state into the watermark —
two-phase like curator/retro, so a crash between scan and advance re-scans instead of silently
dropping a signal.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ...runtime import shared_state
from ...runtime.state import AgentState
from ..pipeline import naming as pipeline_naming
from ..pipeline import ops as pipeline_ops

STATE = AgentState("steward")

# Columns where a long dwell is itself worth a look. "Идеи" (backlog, not yet triaged into Ready)
# and "Done" (terminal) are excluded — sitting there indefinitely is the expected shape, not an
# anomaly.
STALE_COLUMNS = ("Ready", "In progress", "Validate", "Blocked")
STALE_HOURS = float(os.environ.get("TA_STEWARD_STALE_HOURS", "24"))

WORKSPACES_ROOT = shared_state.WORKSPACES_ROOT
_AGENTS_PROJECT = shared_state.AGENTS_PROJECT

# The pipeline dispatcher's own runs.jsonl/resource_health.json used to be reached through
# AgentState("pipeline") — but that resolves STATE_ROOT (runtime/state.py) from the checkout
# running the CURRENT process, and the steward's systemd unit runs entirely inside the steward's
# OWN worktree, a different checkout from the dispatcher's. So that path pointed at a file that
# can never exist there: `_log_signals` on a missing file returned no hits, indistinguishable
# from "checked, nothing new" — the steward was permanently blind to the dispatcher's log
# (triggered-agents-253). The dispatcher's real state lives in ITS OWN named worktree, a sibling
# of the steward's under the same workspaces root — cross that boundary explicitly the same way
# `_orphan_signals` already does for other agents' worktrees via WORKSPACES_ROOT, never through
# AgentState/STATE_ROOT (those are per-process by design, not meant to reach another agent).
# TA_PIPELINE_STATE_DIR overrides the whole thing for tests or a host where the layout diverges.
# The worktree name "pipeline" is fixed by automation.toml and survives redeploy: provision.py
# hard-resets each worktree's CODE to origin/main on every run but never touches the gitignored
# state/ dir underneath it.
def resolve_pipeline_state_dir() -> Path:
    """Recomputed on every call (not baked into a constant at import time) so tests can patch
    WORKSPACES_ROOT and see this follow, the same way _orphan_signals already does."""
    return shared_state.resolve_pipeline_state_dir(WORKSPACES_ROOT)


PIPELINE_RUNS = resolve_pipeline_state_dir() / "runs.jsonl"
PIPELINE_RESOURCE_HEALTH = resolve_pipeline_state_dir() / "resource_health.json"


def _empty_watermark() -> dict:
    return {
        "pipeline_log_lines": 0,
        "notified_blocked": [],
        "notified_stale": {},
        "notified_orphans": [],
        "resource_status": {},
    }


def load_watermark() -> dict:
    mark = _empty_watermark()
    mark.update(STATE.load_watermark())
    return mark


def _log_signals(mark: dict) -> tuple[list[dict], int]:
    """(new warn/error/head-health lines past the watermark's cursor, new total line count).

    A missing PIPELINE_RUNS is NOT "nothing to report" — silently returning `[]` here would be
    indistinguishable from the steady state "checked, no new lines since last time", which is
    exactly the blindness this module exists to catch (a wrong path, the dispatcher's worktree
    never having ticked, a layout change on redeploy). Report it as a synthetic warn hit, so
    has_signal()/precheck wake the head, AND log it straight into the steward's OWN runs.jsonl so
    the gap is durably visible there too, not just inside one scan's in-memory batch
    (2026-07-04, triggered-agents-253)."""
    if not PIPELINE_RUNS.is_file():
        STATE.log_run("pipeline-log-missing", level="warn", path=str(PIPELINE_RUNS))
        return ([{"event": "pipeline-log-missing", "level": "warn", "path": str(PIPELINE_RUNS)}],
                mark["pipeline_log_lines"])
    lines = PIPELINE_RUNS.read_text(encoding="utf-8").splitlines()
    start = mark["pipeline_log_lines"]
    if start > len(lines):
        # The file SHRANK below the cursor: runs.jsonl was truncated/replaced (manual cleanup,
        # rotation, a recreated worktree). The old `min(cursor, len)` clamp skipped the whole new
        # file and the next advance() then baked that gap in permanently — the 2026-07-09 scan
        # missed a head-health flip this way (only resource_flip saved the wake-up). A shrunken
        # file is a NEW file: rescan it from line 0 and surface the reset as a warn hit so the
        # skill knows the log's history restarted (same visibility reasoning as
        # pipeline-log-missing above).
        STATE.log_run("pipeline-log-reset", level="warn", path=str(PIPELINE_RUNS),
                      cursor=start, lines=len(lines))
        hits = [{"event": "pipeline-log-reset", "level": "warn", "path": str(PIPELINE_RUNS),
                 "cursor": start, "lines": len(lines)}]
        start = 0
    else:
        hits = []
    for line in lines[start:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("level") == "warn" or rec.get("result") == "error" or rec.get("event") == "head-health":
            hits.append(rec)
    return hits, len(lines)


def _blocked_signals(mark: dict) -> tuple[list[str], list[str]]:
    """(new Blocked refs since the watermark, every ref currently Blocked)."""
    # A steward report card may intentionally end in Blocked when the run found items that need
    # a human. That report is an accounting artifact, not a fresh anomaly for the next hourly
    # sweep. Stale still catches report cards left in In progress after a dead head.
    blocked = [c["reference"] for c in pipeline_ops.list_cards(column="Blocked")
               if c.get("steward_report") != "1"]
    seen = set(mark["notified_blocked"])
    new = [r for r in blocked if r not in seen]
    return new, blocked


def _stale_signals(mark: dict) -> tuple[list[dict], dict]:
    """Cards past STALE_HOURS in their current column, excluding ones already notified at their
    current date_moved — a card that moves again re-arms the check; one that just sits still,
    already flagged once, does not re-fire every hour. (new stale hits, {ref: date_moved} for
    every card ACTUALLY reported stale this scan or a previous one at its still-current
    date_moved — the next watermark).

    notified_stale must only ever hold refs that crossed the threshold — never a fresh,
    not-yet-stale card. A scan runs on ANY signal (not just a stale one), so if a not-yet-stale
    card's date_moved were written here too, the very next scan's dedup check
    (`notified.get(ref) == moved`) would already match it — permanently immunizing it against
    ever firing once it genuinely does cross the threshold later, since date_moved never changes
    while it just sits still (2026-07-04 review, triggered-agents-244 blocker B1 second round)."""
    now = time.time()
    threshold = STALE_HOURS * 3600
    notified = mark["notified_stale"]
    hits = []
    next_notified = {}
    for column in STALE_COLUMNS:
        for card in pipeline_ops.list_cards(column=column):
            moved = card.get("date_moved")
            if not moved:
                continue
            ref = card["reference"]
            if now - moved < threshold:
                continue  # not stale yet — never recorded, so it can still fire once it is
            if notified.get(ref) == moved:
                next_notified[ref] = moved  # already reported at this exact dwell — stay silent
                continue
            hits.append({"reference": ref, "column": column, "since": moved})
            next_notified[ref] = moved  # just reported — suppress until it moves again
    return hits, next_notified


def _resource_signals(mark: dict) -> tuple[dict, dict]:
    """(resources whose status differs from the watermark — a red->green recovery counts the same
    as a fresh red, both are worth a post-mortem look — current status map for every resource). A
    resource with no prior entry (first-ever scan, or a resource heads.toml just introduced) is a
    new baseline, not a flip — otherwise the very first cold-start scan would "flip" every
    currently-green resource and spawn a head for nothing to report.

    Reads the pipeline dispatcher's OWN resource_health.json cache (PIPELINE_RESOURCE_HEALTH,
    cross-workspace — same reasoning as PIPELINE_RUNS above) instead of calling
    pipeline_health.refresh() to run a fresh probe from here: refresh() executes real
    probes (a haiku CLI ping, an OpenRouter completion) that cost tokens/quota on a TTL, so a
    second independent call from the steward's own worktree would both double that real-world
    cost AND describe a probe the dispatcher itself never saw, on top of writing yet another
    disconnected resource_health.json copy in the steward's own state dir. Reading the
    dispatcher's cache file gives the exact status it actually acted on, for free (2026-07-04
    decision, triggered-agents-253).

    A missing/unreadable/malformed cache file (broken heads.toml, transient I/O, dispatcher never
    ticked yet) keeps the PREVIOUS baseline rather than resetting to {} — same reasoning as the
    old refresh()-failure fallback: resetting to {} would silently erase whatever flip happened on
    the very next real read (2026-07-04 review, triggered-agents-244 note Z3)."""
    try:
        cache = json.loads(PIPELINE_RESOURCE_HEALTH.read_text(encoding="utf-8"))
        current = {rid: entry["status"] for rid, entry in cache.items()}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        current = dict(mark["resource_status"])
    prev = mark["resource_status"]
    changed = {r: s for r, s in current.items() if r in prev and prev[r] != s}
    return changed, current


def _active_card_id_prefixes(project: str) -> set[str]:
    """id-prefixes (`<id>-`, `review-<id>-`) for every active card of `project`, in ANY column —
    including Blocked. The pipeline deliberately leaves a card's worker/reviewer workspace on disk
    with NO cards.json record at all once it reaches Blocked (dispatcher.py's report:blocked path,
    validate.py's Blocked-from-Validate/contrib paths — "left alive for a human to inspect"), so
    matching against cards.json would flag every one of those as a false-positive orphan
    (2026-07-04 review, triggered-agents-244 blocker B1). The board itself, not the dispatcher's
    local cache, is the source of truth for "does an active card still own this workspace" — a
    dedup suffix (naming.dedupe: `<id>-<slug>-2`) still starts with the plain `<id>-` prefix, so
    prefix match survives that without needing the exact slug/dedupe count."""
    prefixes = set()
    for card in pipeline_ops.list_cards(project=project):
        cid = pipeline_naming.card_id(card["reference"])
        prefixes.add(f"{cid}-")
        prefixes.add(f"review-{cid}-")
    return prefixes


# Only names the pipeline itself would have produced (naming.worker_workspace_base /
# reviewer_workspace_base, `<id>-<slug>` / `review-<id>-<slug>`) are orphan candidates. A human
# freely creates worktrees under the same project directory by hand (2026-07-04:
# dnd-simulator/hook-path-filter etc., live sessions with uncommitted work) — those carry no
# card-id prefix by construction, so flagging every non-matching name woke the steward on each
# manual worktree.
_PIPELINE_WS_RE = re.compile(r"^(review-)?\d+-")


def _orphan_signals(mark: dict) -> tuple[list[str], list[str]]:
    """(new orphan workspace paths, every orphan path found this scan) — a directory under
    WORKSPACES_ROOT/<project>/* that is named like a pipeline workspace (_PIPELINE_WS_RE) but
    matches no active card of that project by id-prefix (see _active_card_id_prefixes): a tick
    killed between workspace-create and the cards.json save, a teardown that failed partway, a
    workspace whose card left the board entirely."""
    if not WORKSPACES_ROOT.is_dir():
        return [], []
    orphans = []
    for project_dir in sorted(WORKSPACES_ROOT.iterdir()):
        if not project_dir.is_dir() or project_dir.name == _AGENTS_PROJECT:
            continue
        prefixes = _active_card_id_prefixes(project_dir.name)
        for ws in sorted(project_dir.iterdir()):
            if (ws.is_dir() and _PIPELINE_WS_RE.match(ws.name)
                    and not any(ws.name.startswith(p) for p in prefixes)):
                orphans.append(str(ws))
    notified = set(mark["notified_orphans"])
    new = [o for o in orphans if o not in notified]
    return new, orphans


def scan() -> dict:
    """Everything precheck/the skill need: signals since the watermark, plus the raw state to
    fold into the watermark on advance(). Read-only — never touches the watermark file itself."""
    mark = load_watermark()
    log_hits, log_lines = _log_signals(mark)
    new_blocked, all_blocked = _blocked_signals(mark)
    stale_hits, stale_current = _stale_signals(mark)
    changed_resources, resource_current = _resource_signals(mark)
    new_orphans, all_orphans = _orphan_signals(mark)
    return {
        "signals": {
            "log": log_hits,
            "new_blocked": new_blocked,
            "stale": stale_hits,
            "resource_flip": changed_resources,
            "new_orphan_workspaces": new_orphans,
        },
        "pending": {
            "pipeline_log_lines": log_lines,
            "notified_blocked": all_blocked,
            "notified_stale": stale_current,
            "notified_orphans": all_orphans,
            "resource_status": resource_current,
        },
    }


def has_signal(batch: dict) -> bool:
    s = batch["signals"]
    return bool(s["log"] or s["new_blocked"] or s["stale"] or s["resource_flip"]
                or s["new_orphan_workspaces"])


def render_markdown(batch: dict) -> str:
    s = batch["signals"]
    if not has_signal(batch):
        return "steward: нет сигналов с прошлого watermark.\n"
    lines = ["# steward: сигналы аномалий", ""]
    if s["new_blocked"]:
        lines.append(f"## Новые Blocked ({len(s['new_blocked'])})")
        lines += [f"- {ref}" for ref in s["new_blocked"]]
        lines.append("")
    if s["log"]:
        lines.append(f"## runs.jsonl пайплайна ({len(s['log'])})")
        lines += [f"- {rec.get('ts', '?')} [{rec.get('event', '?')}] "
                  f"{json.dumps(rec, ensure_ascii=False)}" for rec in s["log"]]
        lines.append("")
    if s["stale"]:
        lines.append(f"## Застряло в колонке дольше {STALE_HOURS:g}ч ({len(s['stale'])})")
        for hit in s["stale"]:
            since = datetime.fromtimestamp(hit["since"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"- {hit['reference']} в {hit['column']!r} с {since}")
        lines.append("")
    if s["resource_flip"]:
        lines.append(f"## Флип здоровья ресурса ({len(s['resource_flip'])})")
        lines += [f"- {r}: -> {status}" for r, status in s["resource_flip"].items()]
        lines.append("")
    if s["new_orphan_workspaces"]:
        lines.append(f"## Воркспейс без карточки в полёте ({len(s['new_orphan_workspaces'])})")
        lines += [f"- {p}" for p in s["new_orphan_workspaces"]]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
