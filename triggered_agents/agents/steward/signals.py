"""Deterministic anomaly signals — what the steward's precheck gate and `/steward` skill both
read before anything judges.

Five signal kinds (2026-07-04 design grill, memory id 83 — "стюард присмотр пайплайн дизайн"):
new Blocked card, an unhealthy production dispatcher tick since the steward's watermark, a card
sitting in an active column past STALE_HOURS, a resource health flip, a worker/reviewer workspace
on disk with no in-flight card record. Any one is enough for precheck to spawn the head; finding
none costs nothing (a few Kanboard reads and a couple of local file stats, no LLM).

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

from ...runtime import production_telemetry, shared_state
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

# The pipeline dispatcher's own state is reached across a process boundary, never through
# AgentState("pipeline"): that resolves STATE_ROOT (runtime/state.py) per process, and the
# steward's systemd unit runs in its own worktree with its own environment. A path that can never
# exist there returns no hits, indistinguishable from "checked, nothing new" — the blindness of
# triggered-agents-253.
#
# `resource_health.json` is still the head-health cache the agent dispatch path writes in the
# pipeline worktree. TA_PIPELINE_STATE_DIR overrides it for tests or a host whose layout diverges.
# The worktree name "pipeline" is fixed by automation.toml and survives redeploy: `secretary
# upgrade` fast-forwards each worktree's CODE to origin/main on every run but never touches the
# gitignored state/ dir underneath it.
def resolve_pipeline_state_dir() -> Path:
    """Recomputed on every call (not baked into a constant at import time) so tests can patch
    WORKSPACES_ROOT and see this follow, the same way _orphan_signals already does."""
    return shared_state.resolve_pipeline_state_dir(WORKSPACES_ROOT)


PIPELINE_RESOURCE_HEALTH = resolve_pipeline_state_dir() / "resource_health.json"


def _empty_watermark() -> dict:
    return {
        # None, not 0: no baseline has been taken yet. See _pipeline_tick_signals.
        "pipeline_unhealthy_total": None,
        "notified_blocked": [],
        "notified_stale": {},
        "notified_orphans": [],
        "resource_status": {},
    }


def load_watermark() -> dict:
    mark = _empty_watermark()
    mark.update(STATE.load_watermark())
    return mark


def _pipeline_tick_signals(mark: dict) -> tuple[list[dict], int | None]:
    """(unhealthy production dispatcher ticks since the watermark, new watermark counter).

    The source is the production dispatcher's durable tick telemetry — the record written by the
    timer that actually moves cards on this host (runtime/production_telemetry.py, secretary-833).
    That is where a failing production tick leaves its trace; a log written by anything else is a
    file whose silence cannot be told apart from a healthy pipeline (triggered-agents-253).

    Dedup is keyed on `unhealthy_total`, a counter the dispatcher only ever bumps on an unhealthy
    tick. A healthy tick in between therefore cannot consume an unhealthy one the steward has not
    looked at yet, which a "have I seen the newest record" cursor would do every minute.

    A first scan with no counter in the watermark takes the current one as a BASELINE and reports
    nothing from the ring — same rule `_resource_signals` already applies to a resource it has no
    prior status for. Otherwise a cold start (or the first run after this record was introduced)
    would replay every retained failure as brand new.

    Telemetry that cannot be read is NOT "nothing to report": that silence is indistinguishable
    from a healthy pipeline and is exactly the blindness this module exists to catch. It is
    reported as a synthetic warn hit AND logged into the steward's own runs.jsonl, so the gap is
    durably visible outside one scan's in-memory batch (2026-07-04, triggered-agents-253).
    """
    telemetry = production_telemetry.read()
    if not telemetry.available:
        STATE.log_run(telemetry.unavailable, level="warn", path=str(telemetry.path))
        return ([{"event": telemetry.unavailable, "level": "warn", "path": str(telemetry.path)}],
                mark["pipeline_unhealthy_total"])
    seen = mark["pipeline_unhealthy_total"]
    if seen is None:
        return [], telemetry.unhealthy_total
    hits: list[dict] = []
    if telemetry.unhealthy_total < seen:
        # The counter went BACKWARDS: the state file was replaced (a restore, a rebuilt
        # installation, a hand edit). Its history restarted, so rescan the whole retained ring and
        # surface the reset itself — the same reasoning the old log-reset branch used.
        STATE.log_run("pipeline-telemetry-reset", level="warn", path=str(telemetry.path),
                      cursor=seen, unhealthy_total=telemetry.unhealthy_total)
        hits.append({"event": "pipeline-telemetry-reset", "level": "warn",
                     "path": str(telemetry.path), "cursor": seen,
                     "unhealthy_total": telemetry.unhealthy_total})
        seen = 0
    new_count = telemetry.unhealthy_total - seen
    retained = list(telemetry.unhealthy)[-new_count:] if new_count > 0 else []
    hits += [{"event": "pipeline-tick-unhealthy", "level": "warn", "ts": entry.get("at", ""),
              **entry} for entry in retained]
    dropped = new_count - len(retained)
    if dropped > 0:
        # More unhealthy ticks happened than the ring keeps. The steward must hear that the count
        # is bigger than the batch it just got, or a storm of failures would read as the handful
        # that happened to survive rotation.
        STATE.log_run("pipeline-telemetry-rotated", level="warn", dropped=dropped,
                      unhealthy_total=telemetry.unhealthy_total)
        hits.insert(0, {"event": "pipeline-telemetry-rotated", "level": "warn",
                        "dropped": dropped, "unhealthy_total": telemetry.unhealthy_total})
    return hits, telemetry.unhealthy_total


def ensure_pipeline_baseline(batch: dict) -> None:
    """Persist the pipeline counter baseline a scan just took, before anything else can consume it.

    The baseline itself is described in _pipeline_tick_signals: a scan with no counter in the
    watermark reports nothing and takes the current one instead. That value only ever reached the
    watermark through `advance`, which runs after a steward head was actually dispatched — and the
    normal hour has no signal at all, so precheck exits PRECHECK_SKIP and writes nothing. The next
    unhealthy tick then meets an unset counter again, is read as a first-ever scan again, and is
    suppressed again: every production failure stays invisible forever, which is the opposite of
    what a baseline is for (secretary-833 review, round 2).

    So the baseline is written the moment it is taken, by both entry points that take one. Only
    that one field is touched, and only while it is still unset — the per-kind dedup state stays
    on the two-phase scan/advance contract, where a crash between the two re-scans instead of
    dropping a signal. A concurrent steward run holding the lock needs no help here: its own
    advance persists the same counter, so a contended write is skipped rather than waited on.
    """
    baseline = batch["pending"]["pipeline_unhealthy_total"]
    if baseline is None:
        return  # unreadable telemetry: nothing was measured, so there is no baseline to keep
    try:
        with STATE.lock():
            stored = STATE.load_watermark()
            if stored.get("pipeline_unhealthy_total") is not None:
                return
            stored["pipeline_unhealthy_total"] = baseline
            STATE.save_watermark(stored)
    except SystemExit:
        return


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

    Reads the head-health resource_health.json cache written in the pipeline worktree
    (PIPELINE_RESOURCE_HEALTH, cross-workspace — see the constant above) instead of calling
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
    tick_hits, unhealthy_total = _pipeline_tick_signals(mark)
    new_blocked, all_blocked = _blocked_signals(mark)
    stale_hits, stale_current = _stale_signals(mark)
    changed_resources, resource_current = _resource_signals(mark)
    new_orphans, all_orphans = _orphan_signals(mark)
    return {
        "signals": {
            "pipeline_ticks": tick_hits,
            "new_blocked": new_blocked,
            "stale": stale_hits,
            "resource_flip": changed_resources,
            "new_orphan_workspaces": new_orphans,
        },
        "pending": {
            "pipeline_unhealthy_total": unhealthy_total,
            "notified_blocked": all_blocked,
            "notified_stale": stale_current,
            "notified_orphans": all_orphans,
            "resource_status": resource_current,
        },
    }


def has_signal(batch: dict) -> bool:
    s = batch["signals"]
    return bool(s["pipeline_ticks"] or s["new_blocked"] or s["stale"] or s["resource_flip"]
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
    if s["pipeline_ticks"]:
        lines.append(f"## Тики production dispatcher ({len(s['pipeline_ticks'])})")
        lines += [f"- {rec.get('ts', '?')} [{rec.get('event', '?')}] "
                  f"{json.dumps(rec, ensure_ascii=False)}" for rec in s["pipeline_ticks"]]
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
