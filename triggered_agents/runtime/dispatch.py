"""Singleton terminal driver, shared by every triggered-agent.

Replaces `orca automations run` in the systemd trigger. One agent = one warm terminal in
its worktree, reused across ticks. On a trigger (after precheck passes, under the run lock):

  * no agent terminal          -> create one running the agent's resolved head profile
  * one idle agent terminal    -> `/clear` it and re-send <skill> (warm reuse, kills nothing)
  * ...unless its head is red  -> stop it, start a fresh one on the resolved fallback instead
  * ...unless agent is ephemeral -> stop + tear down instead, start a fresh one (see below)
  * it's busy and fresh        -> leave it working, dispatch nothing
  * it's busy but stuck        -> watchdog: stop the workspace and start one fresh

Why warm reuse and not stop+create every run: Orca retains a dead pty as a ghost tab in the
workspace session after the process exits, so churning a terminal each tick piles up ghost tabs.
Reuse never kills the process, so no ghost is born — steady state stays at one terminal. The rare
kill paths (watchdog, a red idle head, closing legacy duplicates) do leave ghosts, so every run
first reaps them via `session.tabs.close` (`_reap_ghosts`) — the one lever that reaches the
session store; the `terminal` CLI can't. Together: steady state creates none, and any stray gets
swept next tick.

An agent whose automation.toml sets `ephemeral = true` (curator, triggered-agents-445) opts out
of warm reuse entirely: `_is_ephemeral` gates the idle branch above `_reuse_head_is_red`, so an
idle terminal is always stopped and replaced rather than `/clear`-ed, and a stuck one is already
stopped+replaced by the watchdog branch regardless of this flag. Both kill paths now reap their
own ghost tab immediately (not just at the top of the next run), so a completed/stuck/errored
ephemeral run never leaves its PTY or tab behind for someone else to notice. This matters for
curator specifically: its skill writes to shared memory canon off what it reads from its own
session, so a stale warm session risks carrying forward context (or a half-finished write) from a
prior tick into the next one's judgment.

Every one of those kill paths is still only ever REACHED by a future tick's poll, a dispatch.run()
call noticing after the fact that the terminal has gone idle or stuck. For an ephemeral agent that
isn't good enough on its own: `_create_terminal` appends a `; `-separated launcher trailer to the
head's command. On head exit it starts a detached `finalize()` helper (`dispatch --finalize`) that
survives the PTY it stops, then confirms terminal removal and closes the parent tab without a poll.
`run()`'s cleanup-only/watchdog/stray-sweep paths remain the backstop for a terminal that never
reaches its trailer at all (a hard kill, host reboot, Orca itself restarting).

Why not `orca automations run`: it dispatches trigger=manual and spawns a NEW head every tick
(reuse only kicks in for scheduled runs, which don't tick headless), so heads piled up.

"Busy vs idle" is Orca's tui-idle condition; "stuck" is busy with no output for
WATCHDOG_SECONDS. Orca's agent status is known to wedge on 'working' after a silent exit, so a
bare busy check would freeze the agent forever — the watchdog makes "skip when busy" safe.
Dispatch only sends the skill and returns; the head reaches `advance` (same lock) minutes later,
so there's no deadlock.

Every fresh spawn (create, watchdog-restart) goes through `_ensure_claude_ready` first
(`claude_env.ensure_trust`/`ensure_theme`): a head that lands on the folder-trust dialog or the
onboarding theme picker hangs on stdin nobody sends, and never renames its tab away from the
shell default — invisible to the `Claude`-in-title match above, so it's neither reused nor
reaped and just sits there as a silent orphan (found live in the curator workspace: a terminal
stuck at "choose the text style" that `_agent_terminals` couldn't see).

A live terminal never re-resolves its head profile on its own, so every spawn that resolves one
(create, watchdog-restart, red-fallback) records it via `AgentState.save_head_profile` — the only
place idle-reuse can learn which resource the warm terminal is actually running against, since
that can already be a fallback and differ from the agent's static preferred head
(triggered-agents-275).

More invariants, all from PR #95 review rounds (triggered-agents-445):

  * `run(..., cleanup_only=True)` — `ta-gate.sh`'s call on a precheck skip (no new work). Never
    dispatches a skill; for an ephemeral agent it still runs `_cleanup_only` on a finished/stuck
    terminal, because `dispatch` (and thus every kill path above) is otherwise never invoked at
    all on a skip tick — a finished ephemeral run could sit until the next tick that happens to
    have real work, unbounded if that never comes (round 1 review B1). It bails immediately for a
    non-ephemeral agent (retro/steward) — before even constructing `AgentState` or taking
    `state.lock()`, let alone any Orca/board call: their lifecycle is out of scope, so their
    precheck-skip stays the exact zero-side-effect no-op it always was, and a shared gate calling
    `--cleanup-only` on every skip can never turn their quiet tick into a lock-contention
    `SystemExit` (round 2 review B2, hardened round 5 review B1).
    `triggered_agents/__main__.py`'s `pipeline` special case (a deterministic dispatcher with no
    terminal at all) treats `--cleanup-only` the same way, never calling `dispatcher.tick()`
    (round 2 review B1) — a shared gate script means every agent it dispatches to, including a
    skill-less deterministic one, must understand the flag as "there is nothing to do here".
  * Every "stop, then create" path (watchdog-restart, ephemeral-restart, red-fallback) verifies
    the stop actually worked via `_stop_and_confirm` — re-listing terminals rather than trusting
    `terminal stop`'s exit code — before spawning the replacement, and bails without creating if
    it can't confirm. Otherwise a silently-failed stop plus an unconditional create risks two live
    sessions for one singleton agent (round 1 review B3).
  * The "no terminal" branch checks `AgentState.load_terminal_created_at()` against
    `CREATE_VISIBILITY_GRACE_S` before creating: a terminal this same agent just created may not
    be visible in `terminal list` yet, and a second dispatch landing in that gap must not read
    that as "nothing was ever spawned" and create a duplicate (round 1 review B2).
  * Both the "no terminal" branch and `_cleanup_only`, when `_agent_terminals` recognizes nothing,
    still check `_raw_terminal_count` — Orca's unfiltered terminal list for the workspace — and
    sweep (`_stop_and_confirm_workspace_empty` + `_reap_ghosts`) before creating or declaring the
    workspace clean. `_agent_terminals`'s title/handle filter can miss a genuinely live stray (an
    orphan stuck on the shell's default title), which would otherwise survive every tick forever —
    recognized as "empty" and either left alone (cleanup) or piled on top of with a fresh terminal
    (create). `_stop_and_confirm_workspace_empty` (not the narrower `_stop_and_confirm`) verifies
    via the same unfiltered `_raw_terminal_count`, since the filtered view would "confirm" success
    on a stray it could never recognize either way, stopped or not (round 2 review B3).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import tomllib

from . import claude_env, finalizer, orca_rpc, role_env
from .state import AgentState

_REPO_ROOT = Path(__file__).resolve().parents[2]
ORCA = os.environ.get("ORCA_BIN") or shutil.which("orca") or str(Path.home() / ".local/bin/orca")
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))
WATCHDOG_SECONDS = int(os.environ.get("TA_WATCHDOG_SECONDS", "1200"))  # busy + this quiet = stuck
IDLE_PROBE_MS = 2500        # tui-idle satisfied within this = idle; timeout = busy
ORCA_TIMEOUT_S = 20         # never let a hung orca call wedge dispatch while it holds the lock
# How long a just-created terminal gets the benefit of the doubt before "not visible in `terminal
# list` yet" (triggered-agents-445, PR #95 review B2) is read the same as "nothing was ever
# spawned". Generous relative to the lag this guards against (Orca registering a brand new pty),
# tiny relative to any real tick cadence (hourly + jitter).
CREATE_VISIBILITY_GRACE_S = 60
# `finalize` (the detached helper started by an ephemeral head's trailer) retries the run lock this many times,
# sleeping this long between attempts, before deferring to the next tick (triggered-agents-445,
# PR #95 review B2, round 4). A live dispatch tick holds the lock only for the length of its own
# Orca calls, so a short bounded wait lets the finalizer clean up its own completed terminal
# instead of abandoning it the instant it sees contention — while a tick genuinely wedged on the
# lock still hands teardown off rather than spinning forever.
FINALIZE_LOCK_ATTEMPTS = 4
FINALIZE_LOCK_RETRY_S = 2.0
REPORT_VISIBILITY_GAP_SECONDS = 60


@dataclass(frozen=True)
class DispatchCommand:
    skill: str
    launch: str
    profile: str | None
    card_ref: str | None = None


def _orca_json(args: list[str]) -> dict:
    p = subprocess.run([ORCA, *args, "--json"], capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    if p.returncode != 0:
        raise RuntimeError(f"orca {' '.join(args)} failed: {(p.stderr or p.stdout).strip()}")
    data = json.loads(p.stdout)
    return data.get("result", data)


def _orca(args: list[str]) -> None:
    subprocess.run([ORCA, *args], capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)


def _workspace(agent: str) -> str:
    return os.environ.get("TA_WORKSPACE") or str(Path.home() / "orca/workspaces/secretary" / agent)


def _load_spec(agent: str) -> dict:
    return tomllib.loads((_REPO_ROOT / "triggered_agents" / "agents" / agent / "automation.toml").read_text())


def _pipeline_paused() -> bool:
    """Whether the pipeline-wide pause flag (triggered-agents-281, agents/pipeline/pause.py) is
    set — checked first thing in run(), before the ghost reap or any of the four dispatch branches,
    so a paused pipeline never spends a token on steward/curator/retro either: none of them carry
    an in-flight card of their own the way a worker/reviewer head does, so pause has no "let it
    finish its cycle" case here in either mode, soft or hard. Lazy import, same reason as
    _reuse_head_is_red's own agents.pipeline.health import just below — this module is imported at
    process start by every agent, so a top-level import back into agents.pipeline would risk a
    circular import the first time either side changes its own imports. Best-effort: any failure
    (pause.py itself broken) defaults to not-paused rather than silently wedging every agent."""
    try:
        from ..agents.pipeline import pause as pipeline_pause
        return pipeline_pause.is_paused()
    except Exception:
        return False


def _reuse_head_is_red(agent: str, state: AgentState) -> bool:
    """Whether the profile the idle terminal was ACTUALLY launched with is currently sitting on a
    red resource — the check idle-reuse needs before sending into an already-warm terminal, since
    that terminal keeps whatever profile it was spawned with and never re-resolves on its own
    (only a fresh spawn does, via `_launch_cmd`).

    Reads the profile `state` recorded at the terminal's last create/restart/red-fallback
    (`AgentState.load_head_profile`) rather than re-reading `agent`'s static preferred head from
    automation.toml: the two can diverge (the terminal may already be running on a fallback), and
    checking the wrong one either misses a genuinely dead terminal (preferred head recovered while
    the terminal's actual fallback profile went red) or diverts needlessly (preferred head still
    red while the terminal is already happily running its fallback). Falls back to the static
    preferred head when nothing was recorded yet (state predates this tracking).

    Best-effort and defaults to green, matching `_launch_cmd`'s own fallback reasoning: a spec
    with no head, a broken heads.toml, or any resolution failure all mean "nothing to divert
    from", so idle-reuse only ever skips the warm terminal when a red resource is actually
    confirmed (triggered-agents-274, triggered-agents-275).
    """
    try:
        head = _load_spec(agent).get("head")
        if not head:
            return False
        profile = state.load_head_profile() or head
        from ..agents.pipeline import health as pipeline_health
        statuses = pipeline_health.refresh()
        resource = pipeline_health.resource_of(profile)
        return resource is not None and statuses.get(resource, pipeline_health.GREEN) == pipeline_health.RED
    except Exception:
        return False


def _launch_cmd(agent: str, variant: str | None = None,
                card_ref: str | None = None) -> tuple[str, str, str | None]:
    """(skill, full claude launch command, resolved head profile) from the agent's
    automation.toml. The third element is the profile id actually rendered into the launch
    command (None for a spec with no `head`, or when resolution raised) — the caller records it
    via `AgentState.save_head_profile` so a later idle-reuse tick can check the resource this very
    terminal is running against instead of just the agent's static preferred head
    (triggered-agents-275).

    A spec naming a `head` (a profile id in pipeline/heads.toml, e.g. the steward's claude-fable)
    launches through that registry: same adapter/model/fallback machinery a worker/reviewer head
    gets, resolved against this run's live resource health so a red claude-sub falls back to
    claude-opus instead of launching on a rate-limited account. curator/retro name no head
    and keep the bare default-model `claude` invocation they always had. Any failure to resolve
    (a broken heads.toml is itself the kind of anomaly the steward exists to catch) falls back to
    the same bare invocation rather than leaving the agent undispatched for the whole tick.

    `variant` (e.g. the steward's "deep-sweep", triggered-agents-254) reads `skill` from
    `spec["variants"][variant]` instead of the top-level one — a second, differently-scheduled
    mode of the same agent, same worktree/workspace/head, just a different prompt sent into it.

    `card_ref` (triggered-agents-255) appends `--card <ref>` to the skill text BEFORE it is handed
    to the head, the same way a hand-typed `/steward --card ...` would read — so the augmented text
    is what actually gets sent/embedded (heads.render_command reprs the whole prompt as one shell
    argument), not just tacked onto the rendered command afterward where it could land outside the
    quoted prompt.
    """
    spec = _load_spec(agent)
    skill = spec["variants"][variant]["skill"] if variant else spec["skill"]
    if card_ref:
        skill = f"{skill} --card {card_ref}"
    head = spec.get("head")
    bare_claude = role_env.wrap_shell_command(agent, f"claude --dangerously-skip-permissions {skill!r}")
    if not head:
        return skill, bare_claude, None
    try:
        from ..agents.pipeline import health as pipeline_health
        from ..agents.pipeline import heads as pipeline_heads
        statuses = pipeline_health.refresh()
        resolved = pipeline_health.resolve_head(head, statuses) or head
        return skill, pipeline_heads.render_command(resolved, role=agent, prompt=skill), resolved
    except Exception:
        return skill, bare_claude, None


def _steward_report_card(agent: str, variant: str | None) -> str | None:
    """Create the steward's own wake-up report card (project secretary, non-code type,
    straight into In progress, already claimed by itself — see pipeline.ops.create_report_card)
    right before a dispatch actually reaches the head. None for every agent but steward
    (triggered-agents-255): the rest keep their existing dispatch untouched.
    """
    if agent != "steward":
        return None
    from ..agents.pipeline import ops as pipeline_ops
    now = datetime.now(timezone.utc)
    kind = variant or "hourly"
    slug = f"steward-sweep-{now:%Y%m%d-%H%M%S}"
    card = pipeline_ops.create_report_card(
        project=os.environ.get("SECRETARY_META_PROJECT", "secretary"),
        title=f"steward: {kind} sweep {now:%Y-%m-%d %H:%M UTC}",
        slug=slug,
    )
    return card["reference"]


def _is_ephemeral(agent: str) -> bool:
    """Whether `agent`'s automation.toml opts out of warm terminal reuse (curator,
    triggered-agents-445): every tick that finds its terminal idle, or busy-but-stuck past the
    watchdog, tears the whole workspace down and starts a brand new `claude` process instead of
    `/clear`-ing the live one — no provider session or transcript ever survives past the tick
    that produced it. Best-effort like every other spec read in this module: a spec with no
    `ephemeral` field, a missing automation.toml (e.g. a test's synthetic agent name), or any
    parse failure all default to the existing warm-reuse behavior rather than breaking dispatch.
    """
    try:
        return bool(_load_spec(agent).get("ephemeral"))
    except Exception:
        return False


def _dispatch_command(agent: str, variant: str | None) -> DispatchCommand:
    """(skill, launch, resolved head profile) for a dispatch about to actually reach the head —
    the one spot that also creates the steward's report card, so every real dispatch (fresh
    create, watchdog restart, idle reuse) carries one and a busy-skip tick never does (no card,
    nobody to close it)."""
    card_ref = _steward_report_card(agent, variant)
    skill, launch, profile = (_launch_cmd(agent, variant, card_ref=card_ref)
                              if card_ref else _launch_cmd(agent, variant))
    return DispatchCommand(skill, launch, profile, card_ref)


def _terminal_handle_live(ws: str, handle: str) -> bool:
    try:
        terms = _orca_json(["terminal", "list", "--worktree", f"path:{ws}", "--limit", "50"]) \
            .get("terminals", []) or []
    except Exception:
        return False
    return any((t.get("handle") or t.get("id")) == handle for t in terms)


def _fresh_steward_report_in_progress(agent: str, now: float, ws: str,
                                      state: AgentState) -> dict | None:
    """A secondary run guard for steward dispatch.

    Orca terminal creation is not immediately visible in `terminal list` on every host. If two
    timers fire close together, the second dispatch can miss the first terminal and create a
    second report card/head. The report card is already the durable "this run exists" marker, so
    use it as a short-circuit while it is still younger than the steward stale threshold. Once it
    is stale, a later steward run must be allowed through to investigate and close/escalate it.
    """
    if agent != "steward":
        return None
    try:
        from ..agents.pipeline import ops as pipeline_ops
        from ..agents.steward import signals as steward_signals

        threshold = steward_signals.STALE_HOURS * 3600
        meta_project = os.environ.get("SECRETARY_META_PROJECT", "secretary")
        for card in pipeline_ops.list_cards(column="In progress", project=meta_project):
            moved = card.get("date_moved")
            if card.get("steward_report") != "1" or not moved or now - moved >= threshold:
                continue
            active = state.load_active_report() or {}
            handle = active.get("terminal_handle") or ""
            if active.get("reference") != card.get("reference") or not handle:
                state.clear_active_report(card.get("reference"))
                continue
            if _terminal_handle_live(ws, handle) or now - moved < REPORT_VISIBILITY_GAP_SECONDS:
                return card
            state.clear_active_report(card.get("reference"))
    except Exception:
        return None
    return None


def _ensure_claude_ready(ws: str) -> None:
    """Pre-answer folder trust + the onboarding theme picker before a fresh `claude` spawns.

    Without this a head can land on an interactive prompt, wait forever for input nobody sends,
    and never rename its terminal tab away from the shell default — invisible to
    `_agent_terminals`'s title match, so it's reused by nothing and reaped by nothing: an orphan
    every run creates that never dies (seen live in the curator workspace). Best-effort: a config
    hiccup here shouldn't block the tick, just risks the same hang it's meant to prevent.
    """
    try:
        claude_env.ensure_trust(CLAUDE_JSON, ws)
        claude_env.ensure_theme(CLAUDE_JSON)
    except claude_env.ClaudeConfigError as e:
        print(f"dispatch: claude config prep failed ({e})")


def _agent_terminals(ws: str, state: AgentState | None = None) -> list[dict] | None:
    """Live terminals in the workspace running this singleton agent.

    New spawns get an explicit `triggered-agent:<name>` title. The legacy `Claude` match keeps
    already-warm Claude terminals reusable until they are naturally restarted. Codex may rename
    its tab back to the shell cwd after startup, so the latest saved Orca handle is also accepted."""
    try:
        terms = _orca_json(["terminal", "list", "--worktree", f"path:{ws}", "--limit", "50"]).get("terminals", []) or []
    except Exception as exc:
        # An unreadable list is not an empty workspace. Every caller treats None as a deferred
        # decision so an Orca hiccup cannot turn into a second curator or a healthy teardown.
        print(f"dispatch: terminal list unavailable for {ws} ({exc})")
        return None
    saved_handle = state.load_terminal_handle() if state else None
    return [
        t for t in terms
        if (saved_handle and (t.get("handle") or t.get("id")) == saved_handle)
        or (t.get("title") or "").startswith("triggered-agent:")
        or "Claude" in (t.get("title") or "")
    ]


def _raw_terminal_count(ws: str) -> int | None:
    """Every live terminal Orca reports for `ws`, unfiltered by title/handle — unlike
    `_agent_terminals`, this also counts a stray terminal the recognition filter would otherwise
    miss entirely: an orphan stuck on the shell's default title from a past incident (found live
    in the curator workspace once already, see `_ensure_claude_ready`'s docstring), or one that
    simply predates this agent's `triggered-agent:<name>` title convention. An ephemeral agent's
    "no terminal" branch and `_cleanup_only` both need this to actually converge accumulated live
    orphans to zero instead of quietly creating a new terminal alongside one they can't see
    (triggered-agents-445, PR #95 review B3).

    Returns None — "unknown", NOT zero — when the list call itself fails or times out
    (triggered-agents-445, PR #95 review B1, round 4). A confirmation path
    (`_stop_and_confirm_workspace_empty`) must never read an Orca hiccup as "confirmed empty" and
    log a healthy teardown over a terminal/tab that is actually still there; a pre-create/cleanup
    check must not create on top of, or declare clean, a workspace whose real contents it couldn't
    read. Every caller distinguishes the three cases (0 / >0 / None) explicitly."""
    try:
        return len(_orca_json(["terminal", "list", "--worktree", f"path:{ws}", "--limit", "50"])
                   .get("terminals", []) or [])
    except (RuntimeError, subprocess.TimeoutExpired):
        return None


_with_finalizer = finalizer.with_finalizer
spawn_finalizer = finalizer.spawn_finalizer
finalize = finalizer.finalize


def _create_terminal(agent: str, ws: str, launch: str, state: AgentState,
                     profile: str | None) -> str | None:
    generation = None
    if _is_ephemeral(agent):
        # Stamp a fresh monotonic generation and bake it into the terminal's own self-teardown
        # trailer, so a finalizer firing after this terminal's head exits can prove the workspace's
        # current terminal is still this one before stopping it (review B2, round 4).
        generation = state.next_terminal_generation()
        launch = _with_finalizer(agent, launch, generation)
    data = _orca_json(["terminal", "create", "--worktree", f"path:{ws}",
                       "--title", f"triggered-agent:{agent}", "--command", launch])
    term = data.get("terminal", data)
    # created_at only here (never on a plain warm-reuse re-save): see save_terminal_handle's own
    # docstring and the "no terminal" branch's visibility-gap guard below.
    handle = term.get("handle") or term.get("id")
    state.save_terminal_handle(handle, created_at=time.time(),
                               generation=generation)
    state.save_head_profile(profile)
    return handle


def _recover_steward_dispatch_failure(state: AgentState, event: str, cmd: DispatchCommand,
                                      failure: BaseException) -> None:
    if not cmd.card_ref:
        return
    state.clear_active_report(cmd.card_ref)
    body = "steward dispatch failed before the head accepted the report-card run.\n\n" \
           f"failure: {failure}"
    try:
        from ..agents.pipeline import ops as pipeline_ops
        pipeline_ops.move_card("steward", cmd.card_ref, "Done", reason=body)
        state.log_run(event, action="dispatch-recovery", result="done", reference=cmd.card_ref)
    except Exception as recovery_error:
        state.log_run(event, action="dispatch-recovery", result="failed",
                      reference=cmd.card_ref, error=str(recovery_error))


def _spawn_fresh_terminal(agent: str, variant: str | None, ws: str, state: AgentState,
                          event: str) -> DispatchCommand:
    cmd = _dispatch_command(agent, variant)
    try:
        _ensure_claude_ready(ws)
        handle = _create_terminal(agent, ws, cmd.launch, state, cmd.profile)
    except Exception as exc:
        _recover_steward_dispatch_failure(state, event, cmd, exc)
        raise
    state.save_active_report(cmd.card_ref, handle)
    return cmd


def _send_reuse_dispatch(agent: str, variant: str | None, terminal_handle: str,
                         state: AgentState, event: str) -> DispatchCommand:
    cmd = _dispatch_command(agent, variant)
    try:
        _orca(["terminal", "send", "--terminal", terminal_handle,
               "--text", cmd.skill, "--enter"])
    except Exception as exc:
        _recover_steward_dispatch_failure(state, event, cmd, exc)
        raise
    state.save_active_report(cmd.card_ref, terminal_handle)
    return cmd


def _stop_and_confirm(ws: str, state: AgentState) -> bool:
    """Stop every live terminal in `ws` and verify the workspace actually went quiet before the
    caller treats the stop as done. `terminal stop`'s own exit code is not trustworthy enough to
    gate a fresh spawn on (triggered-agents-445, PR #95 review B3): `_orca` doesn't even look at
    it, and Orca itself can report success while a pty lingers. `terminal list` (via
    `_agent_terminals`) is the ground truth every other check in this module already trusts, so
    re-list and require it to come back empty instead. A caller that gets False back must NOT
    proceed to `_create_terminal` — that would risk two live sessions for one singleton agent.

    Only correct for a terminal `_agent_terminals` actually recognized in the first place (every
    call site here is stopping the terminal that branch just matched as the survivor). For a
    stray `_agent_terminals` never recognized to begin with, use `_stop_and_confirm_workspace_
    empty` instead — the filtered view here would "confirm" success on a stray it could never see
    either way, stop or no stop."""
    try:
        _orca(["terminal", "stop", "--worktree", f"path:{ws}"])
        time.sleep(1.0)
        terms = _agent_terminals(ws, state)
    except Exception as exc:
        print(f"dispatch: terminal stop/confirm failed for {ws} ({exc})")
        return False
    return terms == []  # None means the confirmation list was unavailable, not empty


def _stop_and_confirm_workspace_empty(ws: str) -> bool:
    """Stop every live terminal in `ws` and verify via Orca's UNFILTERED terminal list
    (`_raw_terminal_count`) that the workspace is truly empty — for the stray-sweep paths only
    (triggered-agents-445, PR #95 review B3, round 2). `_stop_and_confirm`'s own re-check goes
    through `_agent_terminals`'s title/handle recognition filter, which would trivially read as
    "confirmed empty" for a stray it could never recognize in the first place, stopped or not.

    Only True when the raw list came back AND was empty. A list failure (`_raw_terminal_count`
    returns None, not 0) is NOT a confirmation: the caller must treat it as "could not confirm the
    stop worked" and leave the terminal for the next tick, never log a healthy teardown over a pty
    that may still be live (triggered-agents-445, PR #95 review B1, round 4)."""
    try:
        _orca(["terminal", "stop", "--worktree", f"path:{ws}"])
        time.sleep(1.0)
        return _raw_terminal_count(ws) == 0  # None (list failed) != 0 -> not confirmed
    except Exception as exc:
        print(f"dispatch: terminal stop/confirm failed for {ws} ({exc})")
        return False


def _is_idle(handle: str) -> bool:
    try:
        res = _orca_json(["terminal", "wait", "--terminal", handle, "--for", "tui-idle",
                          "--timeout-ms", str(IDLE_PROBE_MS)])
    except (RuntimeError, subprocess.TimeoutExpired):
        return False
    return bool((res.get("wait") or {}).get("satisfied"))


def _quiet_seconds(term: dict, now: float) -> float:
    last = term.get("lastOutputAt")
    return (now - last / 1000.0) if last else 0.0


def _reap_ghosts(ws: str) -> tuple[int, bool]:
    """Close ghost tabs — ones whose pty died but linger in the workspace session store.

    Returns `(closed, ok)`: `closed` is how many ghost tabs were pruned this call; `ok` is True
    ONLY when the listing succeeded AND every non-ready tab in this workspace closed cleanly. `ok`
    is False when `session.tabs.listAll` was unavailable (Orca restarting) or any individual
    `session.tabs.close` raised — in either case a `pending-handle` artifact may still linger in
    `session.tabs.listAll`. A teardown caller must NOT log a healthy self-teardown/cleanup/restart
    action while `ok` is False: that would claim "zero tabs after completion" over a ghost it never
    actually closed (triggered-agents-445, PR #95 review B1, round 6). The top-of-run opportunistic
    prune ignores `ok` (it just re-reaps next tick); the real teardown paths gate their success
    action on it and record a `*-tab-failed` action instead when it's False.

    `terminal list/stop/close` can't touch these (they only reach live ptys); the persisted
    `tabsByWorktree` keeps them as clutter until `session.tabs.close` prunes them (what the GUI
    tab-× does). Live tabs are status 'ready'; a dead pty leaves 'pending-handle'.
    """
    try:
        snaps = (orca_rpc.call("session.tabs.listAll").get("result") or {}).get("snapshots", []) or []
    except Exception as e:
        # Couldn't even list the tabs: we can't claim the workspace is tab-clean, so report not-ok.
        print(f"dispatch: reap skipped ({e})")
        return 0, False
    closed = 0
    ok = True
    for snap in snaps:
        if snap.get("worktree", "").split("::", 1)[-1] != ws:
            continue
        for tab in snap.get("tabs", []) or []:
            if tab.get("status") != "ready":
                try:
                    orca_rpc.call("session.tabs.close", {"worktree": snap["worktree"], "tabId": tab["parentTabId"]})
                    closed += 1
                except Exception as e:
                    # A tab that fails to close is exactly the leftover the idempotent-cleanup
                    # criterion cares about (triggered-agents-445, PR #95 review B1): surface it as
                    # not-ok so the teardown caller records a failure, not a healthy success.
                    ok = False
                    print(f"dispatch: reap failed to close a tab in {ws} ({e})")
    if not ok:
        return closed, False
    # A successful close RPC is only an acknowledgement, not proof that the tab left Orca's
    # session store. Re-list before reporting a clean teardown; otherwise an accepted-but-lost
    # close could let the next curator start beside the same pending tab.
    try:
        after = (orca_rpc.call("session.tabs.listAll").get("result") or {}).get("snapshots", []) or []
    except Exception as e:
        print(f"dispatch: reap confirmation skipped ({e})")
        return closed, False
    for snap in after:
        if snap.get("worktree", "").split("::", 1)[-1] != ws:
            continue
        if any(tab.get("status") != "ready" for tab in snap.get("tabs", []) or []):
            print(f"dispatch: reap could not confirm all ghost tabs closed in {ws}")
            return closed, False
    return closed, ok


def _cleanup_only(agent: str, ws: str, state: AgentState, event: str, terms: list[dict]) -> int:
    """Tear-down-only pass for an ephemeral agent's finished or stuck terminal, run in place of a
    real dispatch when precheck signalled no new work (triggered-agents-445, PR #95 review B1):
    `ta-gate.sh` skips `dispatch` entirely on a precheck skip, so without this a finished
    ephemeral run's PTY/tab would sit until the next tick that happens to have real work —
    unbounded if that never comes. Never creates a terminal: with nothing new to curate there is
    no skill to hand a fresh session, and creating one anyway would defeat the whole point of
    precheck-gating (spend a token for nothing). Only ever called for an ephemeral agent — `run()`
    bails before this for a non-ephemeral one (retro/steward): their warm terminal, busy or idle,
    stays exactly as a precheck skip always left it; that lifecycle is out of scope for this
    card, and touching it here would spend Orca calls their skip path never used to make
    (PR #95 review B2)."""
    if not terms:
        # `_agent_terminals`'s title/handle filter can miss a stray live terminal that isn't
        # recognized as this agent's own (review B3) -- sweep the whole workspace so accumulated
        # live orphans actually converge to zero on a no-work tick instead of surviving every
        # "nothing recognized" cleanup forever.
        raw = _raw_terminal_count(ws)
        if raw is None:
            # The raw list itself failed (round 4, review B1): "unknown", not "zero". Declaring the
            # workspace clean off an Orca hiccup would log a healthy no-op over a stray that is
            # actually still live -- leave it for the next tick to re-check.
            state.log_run(event, action="cleanup-stray-check-failed")
            print(f"dispatch[{agent}]: cleanup — terminal list unavailable, cannot confirm the "
                  "workspace is clear; leaving it for the next tick")
            return 0
        if raw > 0:
            if not _stop_and_confirm_workspace_empty(ws):
                state.log_run(event, action="cleanup-stray-sweep-failed")
                print(f"dispatch[{agent}]: cleanup could not confirm the workspace is clear of "
                      "stray terminals — leaving it for the next tick")
                return 0
            reaped, ok = _reap_ghosts(ws)
            if not ok:
                state.log_run(event, action="cleanup-stray-swept-tab-failed")
                print(f"dispatch[{agent}]: cleanup — stopped stray terminal(s) but a ghost tab "
                      "would not close; next tick re-reaps")
                return 0
            state.log_run(event, action="cleanup-stray-swept")
            print(f"dispatch[{agent}]: cleanup — swept stray unrecognized terminal(s)"
                  f"{f'; reaped {reaped} ghost(s)' if reaped else ''}, no new work to dispatch")
        return 0
    survivor = max(terms, key=lambda t: t.get("lastOutputAt") or 0)
    if not _is_idle(survivor["handle"]):
        quiet = _quiet_seconds(survivor, time.time())
        if quiet <= WATCHDOG_SECONDS:
            return 0  # still working -- a no-work tick must never touch a live run
        if not _stop_and_confirm(ws, state):
            state.log_run(event, action="cleanup-stop-failed")
            print(f"dispatch[{agent}]: cleanup could not confirm the stuck terminal stopped "
                  "— leaving it for the next tick")
            return 0
        _, ok = _reap_ghosts(ws)
        if not ok:
            state.log_run(event, action="cleanup-watchdog-tab-failed")
            print(f"dispatch[{agent}]: cleanup — stopped stuck terminal but a ghost tab would not "
                  "close; next tick re-reaps")
            return 0
        state.log_run(event, action="cleanup-watchdog-stop")
        print(f"dispatch[{agent}]: cleanup — stopped stuck terminal, no new work to dispatch")
        return 0

    # idle: the previous run already finished; tear it down and leave the workspace empty, there
    # is no new work this tick to hand a fresh session to
    if not _stop_and_confirm(ws, state):
        state.log_run(event, action="cleanup-stop-failed")
        print(f"dispatch[{agent}]: cleanup could not confirm the finished terminal stopped "
              "— leaving it for the next tick")
        return 0
    _, ok = _reap_ghosts(ws)
    if not ok:
        state.log_run(event, action="cleanup-teardown-tab-failed")
        print(f"dispatch[{agent}]: cleanup — stopped finished terminal but a ghost tab would not "
              "close; next tick re-reaps")
        return 0
    state.log_run(event, action="cleanup-teardown")
    print(f"dispatch[{agent}]: cleanup — torn down finished terminal, no new work to dispatch")
    return 0


def run(agent: str, variant: str | None = None, cleanup_only: bool = False) -> int:
    """`variant` selects a differently-scheduled mode of the same agent (e.g. the steward's
    "deep-sweep", triggered-agents-254): a different prompt from `_launch_cmd`, and its own
    runs.jsonl event name (instead of the plain "dispatch" every hourly tick logs) so the two
    wake-up kinds stay distinguishable in the agent's own telemetry.

    `cleanup_only` (triggered-agents-445) is `ta-gate.sh`'s call on a precheck skip: no new work,
    so never dispatch a skill, but still let an ephemeral agent's finished/stuck terminal go
    through `_cleanup_only` instead of sitting untouched until a tick that has real work.

    `_dispatch_command` (not `_launch_cmd` directly) runs only in the three branches below that
    actually put the skill in front of a head (fresh create, watchdog restart, idle reuse) — never
    on a busy-skip, so a tick that dispatches nothing never creates the steward's report card
    either (triggered-agents-255)."""
    if cleanup_only and not _is_ephemeral(agent):
        # A non-ephemeral agent (retro/steward) has no terminal/PTY lifecycle for this pass to
        # clean up -- their warm-reuse lifecycle is out of scope for triggered-agents-445. Bail
        # BEFORE constructing AgentState or taking `state.lock()` (round 5 review B1), let alone
        # any pause check / ghost reap / `terminal list` / steward report lookup: ta-gate.sh calls
        # `--cleanup-only` on every precheck skip for EVERY agent, so acquiring the run lock here
        # would turn a quiet skip that used to print-and-exit-0 into `SystemExit: another run holds
        # the lock` the instant a deterministic helper is running (or a stale lock is left behind).
        # `_is_ephemeral` only reads automation.toml, not the lock/Orca/board. This keeps their
        # precheck skip the exact zero-side-effect no-op it always was before this card.
        return 0
    ws = _workspace(agent)
    state = AgentState(agent)
    event = variant or "dispatch"
    with state.lock():
        if _pipeline_paused():
            state.log_run(event, action="paused")
            print(f"dispatch[{agent}]: pipeline paused — no dispatch")
            return 0
        active_report = _fresh_steward_report_in_progress(agent, time.time(), ws, state)
        if active_report:
            state.log_run(event, action="active-report-skip", reference=active_report["reference"])
            print(
                f"dispatch[{agent}]: active steward report {active_report['reference']} "
                "is still fresh — no dispatch"
            )
            return 0
        reaped, reap_ok = _reap_ghosts(ws)  # prune dead-pty tabs so ghosts never accumulate
        if reaped:
            print(f"dispatch[{agent}]: reaped {reaped} ghost tab(s)")
        terms = _agent_terminals(ws, state)
        if terms is None:
            state.log_run(event, action="terminal-list-failed")
            print(f"dispatch[{agent}]: terminal list unavailable: deferring lifecycle decision")
            return 0

        if cleanup_only:
            return _cleanup_only(agent, ws, state, event, terms)

        if not terms:
            # A terminal this same agent just created can take a moment to show up in `terminal
            # list` (triggered-agents-445, PR #95 review B2). Read that gap the same as "nothing
            # was ever spawned" and a second dispatch landing inside it would create a duplicate
            # curator/head — guard on the timestamp `_create_terminal` just recorded instead.
            last_created = state.load_terminal_created_at()
            if last_created is not None and (time.time() - last_created) < CREATE_VISIBILITY_GRACE_S:
                state.log_run(event, action="recent-create-guard")
                print(f"dispatch[{agent}]: no terminal visible yet but one was created "
                      f"{time.time() - last_created:.1f}s ago — skipping to avoid a duplicate")
                return 0
            if _is_ephemeral(agent):
                if not reap_ok:
                    # The top-of-run reap could NOT confirm this workspace is free of ghost tabs (a
                    # session.tabs.close failed, or session.tabs.listAll was unavailable). The live
                    # PTY of the finished run may be gone (so `_agent_terminals`/`_raw_terminal_count`
                    # read empty), but its `pending-handle` tab still lingers in
                    # session.tabs.listAll. Creating a fresh session now would leave that artifact
                    # sitting right next to a brand new curator — the exact "zero tabs after
                    # completion" breach (triggered-agents-445, PR #95 review B1, round 7). Bail; the
                    # next tick re-reaps before it creates. Restart paths above already do this;
                    # this is the same guard for the no-live-terminal create path.
                    state.log_run(event, action="reap-tab-failed")
                    print(f"dispatch[{agent}]: a ghost tab would not close (or tab list "
                          "unavailable) — not creating a fresh session this tick, next tick re-reaps")
                    return 0
                raw = _raw_terminal_count(ws)
                if raw is None:
                    # The raw list itself failed (round 4, review B1): "unknown", not "zero". We
                    # can't rule out a stray we'd be piling a fresh session on top of, so don't
                    # create this tick -- the next one retries once Orca answers again.
                    state.log_run(event, action="stray-check-failed")
                    print(f"dispatch[{agent}]: terminal list unavailable — skipping create to "
                          "avoid piling a fresh session on a possible stray")
                    return 0
                if raw > 0:
                    # `_agent_terminals` recognized nothing, but Orca still lists a live terminal in
                    # this workspace -- a stray it can't match by title/handle (an orphan from a
                    # past incident, review B3). An ephemeral workspace's whole point is converging
                    # to at most one terminal, so sweep it before creating rather than piling a
                    # fresh session on top of an orphan that would otherwise run forever.
                    if not _stop_and_confirm_workspace_empty(ws):
                        state.log_run(event, action="stray-sweep-failed")
                        print(f"dispatch[{agent}]: could not confirm the workspace is clear of "
                              "stray terminals before creating — leaving it for the next tick")
                        return 0
                    _, ok = _reap_ghosts(ws)
                    if not ok:
                        # Stopped the stray's pty but a ghost tab wouldn't close: creating a fresh
                        # session now would leave the workspace above zero tabs, so bail and let the
                        # next tick re-reap before it creates (review B1, round 6).
                        state.log_run(event, action="stray-sweep-tab-failed")
                        print(f"dispatch[{agent}]: swept stray terminal but a ghost tab would not "
                              "close; not creating this tick, next tick re-reaps")
                        return 0
            cmd = _spawn_fresh_terminal(agent, variant, ws, state, event)
            state.log_run(event, action="created")
            print(f"dispatch[{agent}]: no terminal — created fresh -> {cmd.skill}")
            return 0

        survivor = max(terms, key=lambda t: t.get("lastOutputAt") or 0)
        if not _is_idle(survivor["handle"]):
            quiet = _quiet_seconds(survivor, time.time())
            if quiet <= WATCHDOG_SECONDS:  # a fresh, working agent — don't interrupt or pile on
                state.log_run(event, action="busy-skip")
                print(f"dispatch[{agent}]: agent busy ({int(quiet)}s silent) — left running, no dispatch")
                return 0
            # busy but silent too long -> stuck: sweep and restart, reaping the ghost the stop
            # just made right away rather than leaving it for the top of the next run. Bail
            # without creating if the stop can't be confirmed -- proceeding anyway risks a second
            # live session alongside a stuck one that never actually died (review B3).
            if not _stop_and_confirm(ws, state):
                state.log_run(event, action="watchdog-stop-failed")
                print(f"dispatch[{agent}]: watchdog stop could not confirm the stuck terminal "
                      "is gone — leaving it for the next tick")
                return 0
            _, ok = _reap_ghosts(ws)
            if not ok:
                # Stopped the stuck pty but its ghost tab wouldn't close: don't spawn a replacement
                # next to a lingering tab, bail and let the next tick re-reap first (review B1).
                state.log_run(event, action="watchdog-restart-tab-failed")
                print(f"dispatch[{agent}]: watchdog stopped the stuck terminal but a ghost tab "
                      "would not close; not restarting this tick, next tick re-reaps")
                return 0
            cmd = _spawn_fresh_terminal(agent, variant, ws, state, event)
            state.log_run(event, action="watchdog-restart")
            print(f"dispatch[{agent}]: busy but stuck ({int(quiet)}s silent) — watchdog restart -> {cmd.skill}")
            return 0

        # idle: an ephemeral agent (curator, triggered-agents-445) never reuses a warm terminal —
        # the previous run just finished (successfully or not), so tear its terminal + tab down
        # and start the next tick on a brand new provider session, same shape as the watchdog
        # restart above minus the profile-red gate below (a fresh spawn always re-resolves the
        # head, so there's nothing to divert from).
        if _is_ephemeral(agent):
            if not _stop_and_confirm(ws, state):
                state.log_run(event, action="ephemeral-stop-failed")
                print(f"dispatch[{agent}]: ephemeral teardown could not confirm the finished "
                      "terminal stopped — leaving it for the next tick")
                return 0
            reaped, ok = _reap_ghosts(ws)
            if not ok:
                # Stopped the finished pty but its ghost tab wouldn't close: don't start a fresh
                # session next to a lingering tab, bail and let the next tick re-reap first. The
                # finished head's own finalizer trailer is the usual teardown path anyway; this
                # idle-restart branch is a backstop (review B1, round 6).
                state.log_run(event, action="ephemeral-restart-tab-failed")
                print(f"dispatch[{agent}]: ephemeral teardown stopped the finished terminal but a "
                      "ghost tab would not close; not restarting this tick, next tick re-reaps")
                return 0
            cmd = _spawn_fresh_terminal(agent, variant, ws, state, event)
            state.log_run(event, action="ephemeral-restart")
            tail = f"; reaped {reaped} ghost(s)" if reaped else ""
            print(f"dispatch[{agent}]: ephemeral — torn down finished terminal, fresh session -> {cmd.skill}{tail}")
            return 0

        # idle: a warm terminal keeps whatever profile it was spawned with, so a resource that's
        # gone red since spawn would otherwise get the skill anyway (only a fresh spawn
        # re-resolves). Stop it and start fresh on the resolved fallback instead — same shape as
        # the watchdog restart above — rather than leaving the red terminal running alongside a
        # new one, which would pile up one extra terminal per red tick (triggered-agents-274,
        # triggered-agents-275).
        if _reuse_head_is_red(agent, state):
            if not _stop_and_confirm(ws, state):
                state.log_run(event, action="red-fallback-stop-failed")
                print(f"dispatch[{agent}]: red-fallback stop could not confirm the idle terminal "
                      "stopped — leaving it for the next tick")
                return 0
            cmd = _spawn_fresh_terminal(agent, variant, ws, state, event)
            state.log_run(event, action="reused-red-fallback")
            print(f"dispatch[{agent}]: idle terminal's head is red — stopped, fresh fallback terminal -> {cmd.skill}")
            return 0

        # idle: warm reuse, killing nothing -> no ghost. Close only legacy duplicates (one-time).
        state.save_terminal_handle(survivor.get("handle") or survivor.get("id"))
        extras = [t for t in terms if t["handle"] != survivor["handle"]]
        for t in extras:
            _orca(["terminal", "close", "--terminal", t["handle"]])
        _orca(["terminal", "send", "--terminal", survivor["handle"], "--text", "/clear", "--enter"])
        time.sleep(1.0)  # let /clear settle before the skill lands
        cmd = _send_reuse_dispatch(agent, variant, survivor["handle"], state, event)
        state.log_run(event, action="reused")
        tail = f"; closed {len(extras)} dup(s)" if extras else ""
        print(f"dispatch[{agent}]: reused idle terminal (/clear -> {cmd.skill}){tail}")
        return 0
