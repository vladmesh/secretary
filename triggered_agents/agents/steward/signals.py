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

# Both pipeline signals — unhealthy ticks and resource flips — are reached across a process
# boundary through the production dispatcher's own data plane (runtime/production_telemetry.py),
# never through AgentState("pipeline"): that resolves STATE_ROOT (runtime/state.py) per process,
# and the steward's systemd unit runs in its own worktree with its own environment. A path that
# can never exist there returns no hits, indistinguishable from "checked, nothing new" — the
# blindness of triggered-agents-253, which secretary-833 found again on a worktree copy of
# resource_health.json that the production dispatcher does not write.


def _empty_watermark() -> dict:
    return {
        # None, not 0: no baseline has been taken yet. See _pipeline_tick_signals.
        "pipeline_incident_total": None,
        "pipeline_recovery_total": None,
        # Which telemetry history the counters above were read from — "" while the dispatcher has
        # not stamped one. See _pipeline_tick_signals.
        "pipeline_telemetry_generation": "",
        "notified_blocked": [],
        "notified_stale": {},
        "notified_orphans": [],
        "resource_status": {},
    }


def load_watermark() -> dict:
    mark = _empty_watermark()
    mark.update(STATE.load_watermark())
    return mark


def _pipeline_tick_signals(mark: dict) -> tuple[list[dict], dict]:
    """(pipeline incidents/recoveries since the watermark, the counters to fold into it).

    The source is the production dispatcher's durable tick telemetry — the record written by the
    timer that actually moves cards on this host (runtime/production_telemetry.py, secretary-833).
    That is where a failing production tick leaves its trace; a log written by anything else is a
    file whose silence cannot be told apart from a healthy pipeline (triggered-agents-253).

    The unit reported here is the INCIDENT, not the tick (secretary-839). A Kanboard outage fails
    every tick for as long as it lasts, and per-tick hits made one outage look like a stream of
    separate anomalies whose end nobody ever announced. The dispatcher folds a continuous run of
    unhealthy ticks into one incident and closes it on the first healthy tick
    (`dispatcher_production._record_incident`), so this reads two counters:

      * `incident_total` — one `pipeline-tick-unhealthy` per incident, carrying the tick that
        opened it, so the cause (`backend_unavailable` and friends) is in the hit. Further failing
        ticks of the same incident, and repeated prechecks/scans before `advance`, add nothing.
      * `recovery_total` — one `pipeline-tick-recovered` per closed incident, naming the incident
        it closes, its cause and the healthy tick that ended it. Further healthy ticks add nothing.

    Both counters are monotonic within a generation, so an ordinary tick in between cannot consume
    an event the steward has not looked at yet, which a "have I seen the newest record" cursor
    would do every minute.

    A first scan with no counters in the watermark takes the current ones as a BASELINE and reports
    nothing — same rule `_resource_signals` already applies to a resource it has no prior status
    for. Otherwise a cold start (or the first run after this record was introduced) would replay
    whatever the state file happens to hold as brand new. A watermark written before this contract
    carries no incident counter, so it re-baselines once, on the same rule.

    The counters are only meaningful within one telemetry history, so the watermark stores the
    `generation` they were read from and reports the open incident again the moment that changes. A
    restored or rebuilt state file starts counting again and can land on the numbers the watermark
    already holds, and a counter comparison alone reads that as "nothing new" and drops everything
    the new history reports (secretary-833 review, round 4). A backwards counter is still a reset on
    its own: a host whose dispatcher predates the generation stamp writes none, and the two states
    that leaves — neither side stamped, or only one — are "cannot tell", not "changed", or the
    steward would report a reset on every scan.

    Telemetry that cannot be read is NOT "nothing to report": that silence is indistinguishable
    from a healthy pipeline and is exactly the blindness this module exists to catch. It is
    reported as a synthetic warn hit AND logged into the steward's own runs.jsonl, so the gap is
    durably visible outside one scan's in-memory batch (2026-07-04, triggered-agents-253).
    """
    telemetry = production_telemetry.read()
    if not telemetry.available:
        STATE.log_run(telemetry.unavailable, level="warn", path=str(telemetry.path))
        # The watermark does not move over a source that could not be read.
        return ([{"event": telemetry.unavailable, "level": "warn", "path": str(telemetry.path)}],
                {"pipeline_incident_total": mark["pipeline_incident_total"],
                 "pipeline_recovery_total": mark["pipeline_recovery_total"],
                 "pipeline_telemetry_generation": mark["pipeline_telemetry_generation"]})
    generation = telemetry.generation
    pending = {"pipeline_incident_total": telemetry.incident_total,
               "pipeline_recovery_total": telemetry.recovery_total,
               "pipeline_telemetry_generation": generation}
    seen_incidents = mark["pipeline_incident_total"]
    if seen_incidents is None:
        return [], pending
    seen_recoveries = mark["pipeline_recovery_total"] or 0
    seen_generation = str(mark["pipeline_telemetry_generation"] or "")
    replaced = bool(generation and seen_generation and generation != seen_generation)
    hits: list[dict] = []
    if (replaced or telemetry.incident_total < seen_incidents
            or telemetry.recovery_total < seen_recoveries):
        # The history restarted: the state file was replaced (a restore, a rebuilt installation, a
        # hand edit), either under a new generation or with a counter that went BACKWARDS. Report
        # the reset itself and treat whatever the new history holds as unseen.
        STATE.log_run("pipeline-telemetry-reset", level="warn", path=str(telemetry.path),
                      cursor=seen_incidents, incident_total=telemetry.incident_total,
                      recovery_total=telemetry.recovery_total,
                      generation=generation, seen_generation=seen_generation)
        hits.append({"event": "pipeline-telemetry-reset", "level": "warn",
                     "path": str(telemetry.path), "cursor": seen_incidents,
                     "incident_total": telemetry.incident_total,
                     "recovery_total": telemetry.recovery_total,
                     "generation": generation, "seen_generation": seen_generation})
        seen_incidents = 0
        seen_recoveries = 0
    new_incidents = telemetry.incident_total - seen_incidents
    new_recoveries = telemetry.recovery_total - seen_recoveries
    if new_incidents > 0:
        # The open incident, or — if it has already been closed since the last scan — the closed
        # one the recovery record carries. Either way the failure is reported before its recovery,
        # so the steward never hears that something recovered without hearing what broke.
        incident = telemetry.incident or telemetry.recovery
        if incident:
            hits.append(_incident_hit(incident, new_incidents))
    if new_recoveries > 0 and telemetry.recovery:
        hits.append(_recovery_hit(telemetry.recovery, new_recoveries))
    return hits, pending


def _incident_hit(incident: dict, incidents: int) -> dict:
    opened = incident.get("opened")
    return {
        # The tick that opened the incident, spread whole: status, step, reason, errors and
        # degradations are what an operator reads to tell a board outage from a product bug.
        **(opened if isinstance(opened, dict) else {}),
        "event": "pipeline-tick-unhealthy",
        "level": "warn",
        "ts": incident.get("opened_at", ""),
        "incident": incident.get("id", ""),
        "opened_seq": incident.get("opened_seq"),
        "unhealthy_ticks": incident.get("unhealthy_ticks"),
        "cause": production_telemetry.describe_incident(incident),
        # More than one incident opened between two scans: the newest one is described here, and
        # the count says the steward is not looking at all of them.
        "incidents": incidents,
    }


def _recovery_hit(recovery: dict, recoveries: int) -> dict:
    return {
        "event": "pipeline-tick-recovered",
        "level": "info",
        "ts": recovery.get("recovered_at", ""),
        "incident": recovery.get("id", ""),
        "opened_at": recovery.get("opened_at", ""),
        "opened_seq": recovery.get("opened_seq"),
        "unhealthy_ticks": recovery.get("unhealthy_ticks"),
        "recovered_seq": recovery.get("recovered_seq"),
        "recovered_at": recovery.get("recovered_at", ""),
        "recovered_status": recovery.get("recovered_status", ""),
        # What the incident was, on the recovery hit itself: the steward reads one record and
        # knows both what broke and that it is over.
        "cause": production_telemetry.describe_incident(recovery),
        "recoveries": recoveries,
    }


def ensure_pipeline_baseline(batch: dict) -> None:
    """Persist the pipeline counter baseline a scan just took, before anything else can consume it.

    The baseline itself is described in _pipeline_tick_signals: a scan with no counters in the
    watermark reports nothing and takes the current ones instead. That value only ever reached the
    watermark through `advance`, which runs after a steward head was actually dispatched — and the
    normal hour has no signal at all, so precheck exits PRECHECK_SKIP and writes nothing. The next
    unhealthy tick then meets an unset counter again, is read as a first-ever scan again, and is
    suppressed again: every production failure stays invisible forever, which is the opposite of
    what a baseline is for (secretary-833 review, round 2).

    So the baseline is written the moment it is taken, by both entry points that take one. Only
    the pipeline fields are touched, and only while they are still unset — the per-kind dedup state
    stays on the two-phase scan/advance contract, where a crash between the two re-scans instead of
    dropping a signal. A concurrent steward run holding the lock needs no help here: its own
    advance persists the same counters, so a contended write is skipped rather than waited on.
    """
    baseline = batch["pending"]["pipeline_incident_total"]
    if baseline is None:
        return  # unreadable telemetry: nothing was measured, so there is no baseline to keep
    try:
        with STATE.lock():
            stored = STATE.load_watermark()
            if stored.get("pipeline_incident_total") is not None:
                return
            stored["pipeline_incident_total"] = baseline
            # Incidents and recoveries are one dedup state, taken from one read: a baseline that
            # kept only half of it would replay the recovery of an incident it never reported.
            stored["pipeline_recovery_total"] = batch["pending"].get(
                "pipeline_recovery_total", 0)
            # The counters only mean anything next to the history they were taken from, so the
            # generation is written with them and never after them.
            stored["pipeline_telemetry_generation"] = batch["pending"].get(
                "pipeline_telemetry_generation", "")
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

    Reads the resource_health.json cache the PRODUCTION dispatcher writes next to its own state
    (production_telemetry.resource_health_path) instead of calling pipeline_health.refresh() to run
    a fresh probe from here: refresh() executes real probes (a haiku CLI ping, an OpenRouter
    completion) that cost tokens/quota on a TTL, so a second independent call from the steward's
    own worktree would both double that real-world cost AND describe a probe the dispatcher itself
    never saw, on top of writing yet another disconnected resource_health.json copy in the
    steward's own state dir. Reading the dispatcher's cache file gives the exact status it actually
    acted on, for free (2026-07-04 decision, triggered-agents-253).

    The file is the one the running dispatcher writes, on the same data plane as its tick record —
    the pipeline worktree holds a same-named cache that only the legacy agent path ever fills, so a
    flip the production dispatcher just saw would never reach here (secretary-833 review, round 3).

    A missing/unreadable/malformed cache file (broken heads.toml, transient I/O, dispatcher never
    ticked yet) keeps the PREVIOUS baseline rather than resetting to {} — same reasoning as the
    old refresh()-failure fallback: resetting to {} would silently erase whatever flip happened on
    the very next real read (2026-07-04 review, triggered-agents-244 note Z3)."""
    current = production_telemetry.read_resource_status()
    if current is None:
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
    tick_hits, tick_pending = _pipeline_tick_signals(mark)
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
            **tick_pending,
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
        lines.append(f"## Инциденты тиков production dispatcher ({len(s['pipeline_ticks'])})")
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
