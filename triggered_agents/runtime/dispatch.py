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

Every fresh spawn (create, watchdog-restart) prepares the workspace for its head's own runtime
first, via `_ensure_head_ready`, before any pane is created: a head that lands on a first-run
dialog hangs on stdin nobody sends, and never renames its tab away from the shell default —
invisible to the `Claude`-in-title match above, so it's neither reused nor reaped and just sits
there as a silent orphan (found live in the curator workspace: a terminal stuck at "choose the
text style" that `_agent_terminals` couldn't see). For a `claude` head that is folder trust and the
onboarding theme picker, best-effort; for an interactive `codex` head it is the directory-trust
entry `codex_preflight` writes, and that one is a hard precondition — it fails the spawn before a
pane exists rather than leaving one sitting on a dialog forever.

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

Every terminal this scheduler drives it drives through `SessionHost` (secretary-1416). The pane
verbs — create, list, read, send, stop, close, the tui-idle probe — live in `pane_host` and nowhere
else, and this module holds only what is actually its own: which pane is the survivor, when idle
means reuse and when it means teardown, what a stuck terminal is, and how many kinds of failure a
stop has. `_run_json` is the one subprocess left here, and it runs the argument vectors the host
hands it rather than building any: which words an `orca terminal` call is made of is a fact about
that CLI, and this file has no opinion about it. That is what makes the sprint's grep invariant
(`tests/test_head_command.py`) an assertion about the whole tree with no exceptions in it.

What did NOT move is the reading of those answers. A pane that answers `tui-idle` within
`IDLE_PROBE_MS` is idle here and a probe that times out is busy — two states, not the three the
interactive delivery path classifies, because this scheduler acts on "may I send into it" and not
on why it may not. Likewise the calls whose outcome this module has never checked (`/clear`, the
warm-reuse send, the by-worktree stop, closing a legacy duplicate) still ignore a refusal, and the
ones that gate a spawn still raise: moving a call behind the host changed neither.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import tomllib

from . import claude_env, finalizer, orca_rpc
from .claude_sessions import claude_session_paths
from .codex_preflight import (
    CodexPreflightError,
    preflight_codex_launch,
)
from .head import HeadRun, HeadSpec, RUNTIME_ROLE_ENV, TaskRef, new_run_id, render_head_command
from .pane_host import Pane, SessionHost, safe_command_label, session_host
from .state import AgentState
from .tui_delivery import (
    TuiDeliveryError,
    deliver_interactive_prompt,
    read_pane_text,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
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
# A reused terminal is only useful while an agent REPL still owns the pane. `tui-idle` alone is
# not enough: Orca reports it for a shell that replaced a completed agent too. Keep this bounded
# like the other terminal probes so a broken pane turns into a failed unit rather than a wedged
# scheduler invocation.
REUSE_DELIVERY_TIMEOUT_S = float(os.environ.get("TA_REUSE_DELIVERY_TIMEOUT_S", "12"))
REUSE_DELIVERY_POLL_S = float(os.environ.get("TA_REUSE_DELIVERY_POLL_S", "0.25"))
# How much of a pane's retained output "is an agent REPL still here" is decided on. The panel as
# an operator would see it, not the whole scrollback: a marker from a session that ended hours ago
# must not answer for the pane as it is now.
_SCREEN_READ_LINES = 200
_SHELL_PROMPT_RE = re.compile(
    r"(?:^[^\n]*@[^\n:]+:[^\n]*[#$](?:\s|$)|^(?:bash|zsh|fish|sh)[^\n]*[#$](?:\s|$))",
    re.MULTILINE,
)
_AGENT_REPL_MARKERS = ("Claude Code", "Codex", "Hermes", "❯", "›")


@dataclass(frozen=True)
class DispatchCommand:
    skill: str
    launch: str
    profile: str | None
    card_ref: str | None = None
    # Whether `launch` carries no prompt at all, so `skill` still has to be typed into the head
    # once it is up. Every Codex head is an interactive session and none of them takes a prompt on
    # its command line; a `claude`/`hermes` launch seeds its own and this stays False.
    prompt_after_start: bool = False
    # The registry profile `launch` was rendered from, for an interactive head that needs its
    # workspace prepared before a pane is created. None for every other launch, including the bare
    # fallback invocation a failed resolution falls back to.
    head_profile: dict | None = None


class ReuseDeliveryError(TuiDeliveryError):
    """A warm terminal did not visibly accept its next skill command.

    A kind of the delivery failure the shared interactive path raises, not a second one: a caller
    catching either sees the same thing, a head that was not proven to have taken its prompt.
    """


def _run_json(args: list[str]) -> dict:
    """Run one of `pane_host`'s argument vectors and hand back Orca's result payload.

    The vector arrives complete, binary and `--json` included, and is executed verbatim: the words
    of an `orca terminal` call belong to the module that spells them, and a runner that edited them
    would be a second opinion about the CLI. What stays this scheduler's own is the bound — a tick
    holds the agent's run lock across every one of these calls, so a hung Orca has to fail rather
    than wedge the lock — and the redaction, which happens before the process starts because a
    failure outlives the pane it names.
    """
    p = subprocess.run(args, capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    if p.returncode != 0:
        raise RuntimeError(f"{safe_command_label(args)} failed: {(p.stderr or p.stdout).strip()}")
    data = json.loads(p.stdout)
    return data.get("result", data)


def _unchecked(work: Callable[[], Any]) -> None:
    """Perform a host call this scheduler has never looked at the outcome of.

    Four calls are like this and were like this before they went behind the host: the `/clear` that
    precedes a warm reuse, the warm-reuse send itself, the by-worktree stop (whose success is
    proven by re-listing, never by its own exit code) and the close of a legacy duplicate. Their
    refusals were dropped by the fire-and-forget runner that issued them, and dropping them here
    keeps that: a `/clear` Orca declined must not become an exception that skips the dispatch it
    was clearing for, and a stop must still be judged by the inventory that follows it.

    A refusal and an answer that cannot be read are both ignored, which is exactly what a runner
    that never parsed the output did. A call that hangs is not: `_run_json`'s timeout still reaches
    the caller, so a wedged Orca fails the tick instead of being waited on inside the run lock.
    """
    try:
        work()
    except (RuntimeError, ValueError):
        return


def _terminal_screen(handle: str, *, host: SessionHost) -> str:
    """Rendered terminal text, or an empty string when Orca cannot provide it.

    This deliberately reads the panel instead of inferring liveness from the terminal record.
    A completed agent leaves a perfectly live PTY behind, now owned by bash.

    The read, the tail-or-text shapes Orca answers it in and the ANSI stripping are the delivery
    path's `read_pane_text`, not a second reading of them here. The window stays this caller's:
    200 lines is what "is an agent REPL on screen" has always been decided on, and a whole
    retained scrollback would let a marker from a session that ended hours ago answer for the
    pane as it is now.
    """
    return read_pane_text(handle, host=host, limit=_SCREEN_READ_LINES)


def _agent_repl_visible(handle: str, *, host: SessionHost) -> bool:
    """Whether the observed panel is an agent REPL, not the shell it may have returned to.

    The prompt glyphs cover the supported interactive runtimes.  We require a positive REPL
    marker as well as the absence of a shell prompt at the bottom of the panel: unknown or
    unreadable screens are unsafe to receive a slash command and therefore take the
    fresh-terminal path. Tool output can legitimately contain a shell prompt, so it must not
    classify the whole scrollback as a shell.
    """
    screen = _terminal_screen(handle, host=host)
    if not screen or _shell_prompt_at_tail(screen):
        return False
    return any(marker in screen for marker in _AGENT_REPL_MARKERS)


def _shell_prompt_at_tail(screen: str) -> bool:
    """Whether the last non-empty terminal line is a shell prompt."""
    for line in reversed(screen.splitlines()):
        if line.strip():
            return bool(_SHELL_PROMPT_RE.search(line))
    return False


def _claude_projects_root() -> Path:
    configured = os.environ.get("TA_CLAUDE_PROJECTS")
    return Path(configured) if configured else Path.home() / ".claude" / "projects"


def _claude_session_paths_for(workspace: str):
    """Yield Claude session logs for one workspace without scanning other projects.

    The directory name is Claude Code's, not ours: see `runtime/claude_sessions`, which owns the
    one reading of that convention both this driver and the dispatcher's delivery boundary use.
    """
    return claude_session_paths(workspace, root=_claude_projects_root())


def _claude_user_turn_after(workspace: str, since: float) -> bool:
    """Whether Claude durably recorded a new user turn for this workspace after ``since``."""
    for path in _claude_session_paths_for(workspace):
        try:
            if path.stat().st_mtime <= since:
                continue
            with path.open(encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict) or record.get("type") != "user":
                        continue
                    timestamp = record.get("timestamp")
                    if not isinstance(timestamp, str):
                        continue
                    try:
                        recorded_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    if recorded_at > since:
                        return True
        except OSError:
            continue
    return False


def _codex_turn_after(workspace: str, since: float) -> bool:
    """Whether the Codex session for this workspace wrote anything after ``since``.

    Codex persists its turns as rollout JSONL under CODEX_HOME, so the file moving after the send
    boundary is the same kind of durable proof `_claude_user_turn_after` reads for Claude — it is
    the head having taken the prompt into a turn, not the terminal having accepted keystrokes.
    """
    try:
        from ..agents.pipeline import codex_sessions
        latest = codex_sessions.latest_activity_for(workspace)
    except Exception:
        return False
    return latest is not None and latest > since


def _confirm_delivery(handle: str, workspace: str, sent_at: float, *, host: SessionHost) -> None:
    """Wait until the head durably records the command just sent into its live session.

    For a head whose launch command seeds its own prompt, which is a Claude or Hermes one. An
    interactive head is delivered to — and confirmed — through the shared interactive path
    instead, so nothing here decides between two providers' records any more.
    """
    deadline = time.monotonic() + REUSE_DELIVERY_TIMEOUT_S
    last_reason = "no-user-turn"
    while time.monotonic() < deadline:
        if _claude_user_turn_after(workspace, sent_at):
            return
        screen = _terminal_screen(handle, host=host)
        if not screen:
            last_reason = "panel-unreadable"
        elif _shell_prompt_at_tail(screen):
            last_reason = "agent-repl-lost"
            break
        elif not any(marker in screen for marker in _AGENT_REPL_MARKERS):
            last_reason = "agent-repl-not-visible"
        time.sleep(max(REUSE_DELIVERY_POLL_S, 0.01))
    raise ReuseDeliveryError(
        f"delivery was not confirmed after {REUSE_DELIVERY_TIMEOUT_S:.1f}s "
        f"(reason={last_reason})"
    )


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
    circular import the first time either side changes its own imports. Any failure is a pause:
    dispatching while an operator's stop condition cannot be read is worse than deferring one tick.
    The warning goes to the service log every affected tick until the state is repaired."""
    try:
        from ..agents.pipeline import pause as pipeline_pause
        return pipeline_pause.is_paused()
    except Exception as exc:
        print(
            "dispatch: pipeline pause state is unreadable; refusing dispatch "
            f"({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return True


def _preferred_head(agent: str, spec: dict) -> str | None:
    """The head this agent launches on: the selected registry's role default for it.

    Role routing belongs to the installation, not to the product's automation spec — the same
    `[role_defaults]` table the dispatcher routes worker, reviewer and observer through also names
    curator's, retro's and steward's head, so one registry generation decides all six. The spec's
    own `head` stays as the last resort for a registry that routes this role nowhere.

    That last resort is a head id written down in the product, not in the registry it has to be
    found in, so it goes through the registry's own resolution: an agent pinned to a Codex id from
    before every Codex head became interactive still reaches the equivalent profile the
    installation publishes now, instead of falling back to a bare `claude` invocation nobody chose.

    Resolution refusing that id reaches the caller rather than becoming the bare invocation: a
    Codex-pinned service agent whose registry has no interactive Codex head left for that name is
    a dispatch that must not happen, not one to quietly run on another family.
    """
    try:
        from ..agents.pipeline import heads as pipeline_heads
        registry = pipeline_heads.load_registry()
    except Exception:
        return spec.get("head")
    routed = registry.role_default(agent)
    if routed:
        return routed
    fallback = spec.get("head")
    return registry.resolve(fallback) if fallback else fallback


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
        head = _preferred_head(agent, _load_spec(agent))
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
                card_ref: str | None = None) -> tuple[str, str, str | None, bool, dict | None]:
    """(skill, full launch command, resolved head profile, prompt-after-start, profile data) from
    the agent's automation.toml. The third element is the profile id actually rendered into the launch
    command (None for a spec with no `head`, or when resolution raised) — the caller records it
    via `AgentState.save_head_profile` so a later idle-reuse tick can check the resource this very
    terminal is running against instead of just the agent's static preferred head
    (triggered-agents-275).

    The head comes from `_preferred_head` — the selected registry's role default for this agent,
    else the spec's own `head` — and launches through that same registry: same adapter/model/
    fallback machinery a worker/reviewer head gets, resolved against this run's live resource
    health so a red claude-sub falls back instead of launching on a rate-limited account. An agent
    routed nowhere at all keeps the bare default-model `claude` invocation. Any failure to resolve
    (a broken registry is itself the kind of anomaly the steward exists to catch) falls back to
    the same bare invocation rather than leaving the agent undispatched for the whole tick.

    `variant` (e.g. the steward's "deep-sweep", triggered-agents-254) reads `skill` from
    `spec["variants"][variant]` instead of the top-level one — a second, differently-scheduled
    mode of the same agent, same worktree/workspace/head, just a different prompt sent into it.

    `card_ref` (triggered-agents-255) appends `--card <ref>` to the skill text BEFORE it is handed
    to the head, the same way a hand-typed `/steward --card ...` would read — so the augmented text
    is what actually gets sent (or embedded, for a head whose command seeds its own prompt), not
    just tacked onto the rendered command afterward where it could land outside the quoted prompt.

    The fourth element is that distinction: a Codex head is an interactive session whose command
    carries no prompt, so the caller types `skill` into it once the pane is up.

    The fifth is the resolved profile's own data, carried for exactly one reason: an interactive
    head has to have its workspace prepared before its pane exists, and the preflight that does it
    reads the CODEX_HOME from the profile the command was rendered from. Resolving it a second time
    at the call site could answer differently — this run's resource health has already chosen a
    fallback here — and a preflight run against a different home than the launch names would write
    trust the head never reads.
    """
    spec = _load_spec(agent)
    skill = spec["variants"][variant]["skill"] if variant else spec["skill"]
    if card_ref:
        skill = f"{skill} --card {card_ref}"
    head = _preferred_head(agent, spec)
    # The head a registry routes this agent to is the ordinary case; a bare default-model `claude`
    # is what an agent routed nowhere, or a registry that will not load, still gets dispatched
    # with. Both are rendered by the same renderer from a profile — the fallback's profile is just
    # the emptiest one there is — so a background agent's command cannot drift from a pipeline
    # head's by being assembled somewhere else.
    bare_claude = render_head_command(
        {"adapter": "claude"}, prompt=skill, role=agent, binding=RUNTIME_ROLE_ENV,
    ).command
    if not head:
        return skill, bare_claude, None, False, None
    try:
        from ..agents.pipeline import health as pipeline_health
        from ..agents.pipeline import heads as pipeline_heads
        statuses = pipeline_health.refresh()
        resolved = pipeline_health.resolve_head(head, statuses) or head
        registry = pipeline_heads.load_registry()
        profile = registry.profile(resolved)
        rendered = render_head_command(
            profile, prompt=skill, role=agent, workspace=_workspace(agent),
            binding=RUNTIME_ROLE_ENV,
        )
        return (skill, rendered.command, resolved, rendered.prompt_after_start, profile)
    except Exception:
        return skill, bare_claude, None, False, None


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
    skill, launch, profile, after_start, profile_data = (
        _launch_cmd(agent, variant, card_ref=card_ref) if card_ref else _launch_cmd(agent, variant)
    )
    return DispatchCommand(skill, launch, profile, card_ref, prompt_after_start=after_start,
                           head_profile=profile_data)


def _terminal_handle_live(ws: str, handle: str, *, host: SessionHost) -> bool:
    try:
        panes = host.panes(ws)
    except Exception:
        return False
    return any(pane.handle == handle for pane in panes)


def _fresh_steward_report_in_progress(agent: str, now: float, ws: str, state: AgentState, *,
                                      host: SessionHost) -> dict | None:
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
            if _terminal_handle_live(ws, handle, host=host) \
                    or now - moved < REPORT_VISIBILITY_GAP_SECONDS:
                return card
            state.clear_active_report(card.get("reference"))
    except Exception:
        return None
    return None


def _ensure_head_ready(ws: str, cmd: DispatchCommand, *, role: str = "service") -> None:
    """Prepare `ws` for the head about to be spawned into it, on that head's own runtime.

    A service head is a head like a pipeline worker is, and the first-run question its runtime asks
    is the one thing that can make a fresh pane never come up at all. Which question that is
    depends on the runtime: a `claude` head is asked about folder trust and the theme picker, an
    interactive `codex` head about directory trust. So this is the one place that branches on it,
    right before `_create_terminal`, which is the ordering `codex_preflight` states for every
    interactive Codex head and the Secretary dispatcher keeps too.

    The two failure modes are deliberately not the same. Claude's preparation stays best-effort:
    it has always been, a config hiccup there risks the hang it prevents but does not guarantee
    one, and nothing in this module has ever gated a tick on it. The Codex preflight is a hard
    precondition — without the trust entry the pane cannot reach readiness, so spawning one anyway
    would create exactly the wedged head this exists to prevent — and it raises, before any pane
    is created.
    """
    if cmd.prompt_after_start and str((cmd.head_profile or {}).get("adapter") or "") == "codex":
        # This service path does not own a dispatcher record, but it still crosses the shared
        # pre-pane boundary.  It attaches the same advisory provider telemetry as a pipeline
        # head while keeping the Codex trust write as the only hard preparation requirement.
        spec = HeadSpec.from_profile(str(cmd.profile or role), cmd.head_profile)
        preflight_codex_launch(
            cmd.head_profile,
            ws,
            HeadRun(
                run_id=new_run_id(),
                spec=spec,
                workspace=ws,
                task_ref=TaskRef.standing(role),
                role=role,
            ),
        )
        return
    _ensure_claude_ready(ws)


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


def _agent_terminals(ws: str, state: AgentState | None = None, *,
                     host: SessionHost) -> list[Pane] | None:
    """Live terminals in the workspace running this singleton agent.

    New spawns get an explicit `triggered-agent:<name>` title. The legacy `Claude` match keeps
    already-warm Claude terminals reusable until they are naturally restarted. Codex may rename
    its tab back to the shell cwd after startup, so the latest saved Orca handle is also accepted.

    The inventory arrives as `Pane`s, so the title this recognises by and the last-output clock the
    watchdog reads are fields of the pane rather than keys this module knows the spelling of."""
    try:
        panes = host.panes(ws)
    except Exception as exc:
        # An unreadable list is not an empty workspace. Every caller treats None as a deferred
        # decision so an Orca hiccup cannot turn into a second curator or a healthy teardown.
        print(f"dispatch: terminal list unavailable for {ws} ({exc})")
        return None
    saved_handle = state.load_terminal_handle() if state else None
    return [
        pane for pane in panes
        if (saved_handle and pane.handle == saved_handle)
        or pane.title.startswith("triggered-agent:")
        or "Claude" in pane.title
    ]


def _raw_terminal_count(ws: str, *, host: SessionHost) -> int | None:
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
    read. Every caller distinguishes the three cases (0 / >0 / None) explicitly. An inventory the
    host cannot parse refuses rather than answering none, which lands here as the same "unknown"."""
    try:
        return len(host.panes(ws))
    except (RuntimeError, subprocess.TimeoutExpired):
        return None


_with_finalizer = finalizer.with_finalizer
spawn_finalizer = finalizer.spawn_finalizer
finalize = finalizer.finalize


def _create_terminal(agent: str, ws: str, launch: str, state: AgentState,
                     profile: str | None, *, host: SessionHost) -> str:
    """Open this agent's pane and record what was opened.

    The pane comes back named: `open_pane` refuses a create Orca answered without a handle instead
    of returning one, so a spawn that could not be addressed fails here rather than being recorded
    under a null handle and then delivered to. That is the one place this move made a swallow into
    a refusal, and it is the right way round — a handle nothing can address is not a terminal this
    tick may claim to have created (secretary-1416)."""
    generation = None
    if _is_ephemeral(agent):
        # Stamp a fresh monotonic generation and bake it into the terminal's own self-teardown
        # trailer, so a finalizer firing after this terminal's head exits can prove the workspace's
        # current terminal is still this one before stopping it (review B2, round 4).
        generation = state.next_terminal_generation()
        launch = _with_finalizer(agent, launch, generation)
    pane = host.open_pane(ws, f"triggered-agent:{agent}", launch)
    # created_at only here (never on a plain warm-reuse re-save): see save_terminal_handle's own
    # docstring and the "no terminal" branch's visibility-gap guard below.
    state.save_terminal_handle(pane.handle, created_at=time.time(),
                               generation=generation)
    state.save_head_profile(profile)
    return pane.handle


def _recover_steward_dispatch_failure(state: AgentState, event: str, cmd: DispatchCommand,
                                      failure: BaseException) -> None:
    """Close out a steward report card whose head was brought up but never took the run.

    Only for a failure after the pane exists. A workspace that could not be prepared at all is
    escalated instead, by `_escalate_steward_preflight_failure`: nothing ran, so there is nothing
    to close.
    """
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


def _escalate_steward_preflight_failure(state: AgentState, event: str, cmd: DispatchCommand,
                                        failure: BaseException) -> None:
    """Put a steward report card in front of a human when its workspace could not be prepared.

    The preflight fails before any pane is created, so no head has seen this card and no sweep has
    happened. Closing it as Done — what a post-pane dispatch failure does — would record a sweep
    that never ran and consume the card that says one was due, and the condition is not one a
    later tick heals on its own: an untrusted repository root or a codex config the launcher may
    not rewrite stays that way until somebody changes it. Blocked is the board's own "give up,
    wait for a human" state and the steward's own escape hatch, so the card lands there with the
    preflight's reason attached, visible rather than silently swallowed.

    A card that cannot even be moved leaves its reason in the run log, the same fallback the
    close-out path takes: the tick is failing either way, and a recovery that raises on its own
    would replace the real cause with its own.
    """
    if not cmd.card_ref:
        return
    state.clear_active_report(cmd.card_ref)
    body = "steward dispatch could not prepare the head workspace, so no head was started " \
           "and no sweep ran.\n\n" \
           f"failure: {failure}"
    try:
        from ..agents.pipeline import ops as pipeline_ops
        pipeline_ops.move_card("steward", cmd.card_ref, "Blocked", reason=body)
        state.log_run(event, action="dispatch-preflight", result="blocked", reference=cmd.card_ref,
                      error=str(failure))
    except Exception as escalation_error:
        state.log_run(event, action="dispatch-preflight", result="failed", reference=cmd.card_ref,
                      error=f"{failure} (escalation failed: {escalation_error})")


def _deliver_interactive_skill(handle: str, workspace: str, skill: str, *,
                               host: SessionHost) -> None:
    """Put a service head's skill in front of it, on the product's one interactive delivery path.

    A Codex head starts with an empty composer, fresh or warm: nothing has been asked of it until
    this lands, so the dispatch is not finished when `terminal create` (or `/clear`) returns. The
    delivery itself — waiting for the pane to answer Orca's readiness probe, sending, re-entering
    a prompt a dialog swallowed, and failing when the pane cannot be probed at all — is the same
    primitive a worker, a reviewer and an observer are given their prompt through. The criterion
    this side proves it with stays this side's: Codex having durably recorded the turn for this
    workspace after the send boundary.

    A failure raises, exactly like a warm reuse that was never taken: the terminal stays up and
    idle, so the next tick recognises it and re-sends the skill through the reuse path rather than
    piling a second head beside a silent one.
    """
    deliver_interactive_prompt(
        handle,
        skill,
        host=host,
        adapter="codex",
        confirm=lambda sent_at: _codex_turn_after(workspace, sent_at),
    )


def _spawn_fresh_terminal(agent: str, variant: str | None, ws: str, state: AgentState,
                          event: str, *, host: SessionHost) -> DispatchCommand:
    """Bring a fresh head up in `ws`: prepare the workspace, create the pane, deliver the skill.

    The preparation is deliberately outside the recovery below. Once a pane exists the head may
    have started work, so a failure after that point is a run that has to be closed out; a failure
    before it started nothing at all, and closing a report card for it would record a sweep that
    never happened.
    """
    cmd = _dispatch_command(agent, variant)
    try:
        _ensure_head_ready(ws, cmd, role=agent)
    except CodexPreflightError as exc:
        _escalate_steward_preflight_failure(state, event, cmd, exc)
        raise
    try:
        handle = _create_terminal(agent, ws, cmd.launch, state, cmd.profile, host=host)
        if cmd.prompt_after_start:
            _deliver_interactive_skill(handle, ws, cmd.skill, host=host)
    except Exception as exc:
        _recover_steward_dispatch_failure(state, event, cmd, exc)
        raise
    state.save_active_report(cmd.card_ref, handle)
    return cmd


def _send_reuse_dispatch(agent: str, variant: str | None, terminal_handle: str, workspace: str,
                         state: AgentState, event: str, *, host: SessionHost) -> DispatchCommand:
    cmd = _dispatch_command(agent, variant)
    try:
        if cmd.prompt_after_start:
            # An interactive head is prompted the one way the product prompts one, whether this
            # is the terminal's first skill or its fifth.
            _deliver_interactive_skill(terminal_handle, workspace, cmd.skill, host=host)
        else:
            sent_at = time.time()
            # The send stays unchecked and the confirmation stays the proof: what makes this
            # dispatch real is the head's own record of the turn, never the send's exit code.
            _unchecked(lambda: host.send(terminal_handle, cmd.skill, enter=True))
            _confirm_delivery(terminal_handle, workspace, sent_at, host=host)
    except Exception as exc:
        _recover_steward_dispatch_failure(state, event, cmd, exc)
        raise
    state.save_active_report(cmd.card_ref, terminal_handle)
    return cmd


def _stop_and_confirm(ws: str, state: AgentState, *, host: SessionHost) -> bool:
    """Stop every live terminal in `ws` and verify the workspace actually went quiet before the
    caller treats the stop as done. `terminal stop`'s own exit code is not trustworthy enough to
    gate a fresh spawn on (triggered-agents-445, PR #95 review B3): the stop is issued through
    `_unchecked` for exactly that reason, and Orca itself can report success while a pty lingers.
    The stop is by worktree, not by pane — it is the whole workspace going quiet that the
    confirmation below then reads, and stopping the survivor alone would leave the others.
    `terminal list` (via
    `_agent_terminals`) is the ground truth every other check in this module already trusts, so
    re-list and require it to come back empty instead. A caller that gets False back must NOT
    proceed to `_create_terminal` — that would risk two live sessions for one singleton agent.

    Only correct for a terminal `_agent_terminals` actually recognized in the first place (every
    call site here is stopping the terminal that branch just matched as the survivor). For a
    stray `_agent_terminals` never recognized to begin with, use `_stop_and_confirm_workspace_
    empty` instead — the filtered view here would "confirm" success on a stray it could never see
    either way, stop or no stop."""
    try:
        _unchecked(lambda: host.stop_workspace(ws))
        time.sleep(1.0)
        terms = _agent_terminals(ws, state, host=host)
    except Exception as exc:
        print(f"dispatch: terminal stop/confirm failed for {ws} ({exc})")
        return False
    return terms == []  # None means the confirmation list was unavailable, not empty


def _stop_and_confirm_workspace_empty(ws: str, *, host: SessionHost) -> bool:
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
        _unchecked(lambda: host.stop_workspace(ws))
        time.sleep(1.0)
        return _raw_terminal_count(ws, host=host) == 0  # None (list failed) != 0 -> not confirmed
    except Exception as exc:
        print(f"dispatch: terminal stop/confirm failed for {ws} ({exc})")
        return False


def _is_idle(handle: str, *, host: SessionHost) -> bool:
    """Whether the pane will take input now, in the two states this scheduler acts on.

    The probe is the host's `tui-idle` wait with this module's own `IDLE_PROBE_MS` budget, and the
    reading is unchanged: an answer that says satisfied is idle, and everything else — a refusal,
    the probe timing out, an answer that says anything else — is busy. Deliberately not the three
    states `tui_delivery.terminal_readiness` classifies for a delivery: a tick that cannot ask the
    question and a tick looking at a working head both do the same thing here, which is leave the
    terminal alone, so a third state would be a distinction with no branch behind it.
    """
    try:
        res = host.wait_idle(handle, timeout_ms=IDLE_PROBE_MS)
    except (RuntimeError, subprocess.TimeoutExpired):
        return False
    return bool((res.get("wait") or {}).get("satisfied"))


def _quiet_seconds(pane: Pane, now: float) -> float:
    """How long this pane has been silent, from the clock the inventory carries."""
    return (now - pane.last_output_at) if pane.last_output_at else 0.0


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


def _cleanup_only(agent: str, ws: str, state: AgentState, event: str, terms: list[Pane], *,
                  host: SessionHost) -> int:
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
        raw = _raw_terminal_count(ws, host=host)
        if raw is None:
            # The raw list itself failed (round 4, review B1): "unknown", not "zero". Declaring the
            # workspace clean off an Orca hiccup would log a healthy no-op over a stray that is
            # actually still live -- leave it for the next tick to re-check.
            state.log_run(event, action="cleanup-stray-check-failed")
            print(f"dispatch[{agent}]: cleanup — terminal list unavailable, cannot confirm the "
                  "workspace is clear; leaving it for the next tick")
            return 0
        if raw > 0:
            if not _stop_and_confirm_workspace_empty(ws, host=host):
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
    survivor = max(terms, key=lambda pane: pane.last_output_at)
    if not _is_idle(survivor.handle, host=host):
        quiet = _quiet_seconds(survivor, time.time())
        if quiet <= WATCHDOG_SECONDS:
            return 0  # still working -- a no-work tick must never touch a live run
        if not _stop_and_confirm(ws, state, host=host):
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
    if not _stop_and_confirm(ws, state, host=host):
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


def run(agent: str, variant: str | None = None, cleanup_only: bool = False, *,
        host: SessionHost | None = None) -> int:
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
    # One session manager for the whole tick, resolved here and passed down: every pane this
    # tick lists, probes, sends into and stops is the same one, and a helper cannot quietly
    # open a second route to Orca of its own.
    host = session_host(_run_json) if host is None else host
    with state.lock():
        if _pipeline_paused():
            state.log_run(event, action="paused")
            print(f"dispatch[{agent}]: pipeline paused — no dispatch")
            return 0
        active_report = _fresh_steward_report_in_progress(agent, time.time(), ws, state, host=host)
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
        terms = _agent_terminals(ws, state, host=host)
        if terms is None:
            state.log_run(event, action="terminal-list-failed")
            print(f"dispatch[{agent}]: terminal list unavailable: deferring lifecycle decision")
            return 0

        if cleanup_only:
            return _cleanup_only(agent, ws, state, event, terms, host=host)

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
                raw = _raw_terminal_count(ws, host=host)
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
                    if not _stop_and_confirm_workspace_empty(ws, host=host):
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
            cmd = _spawn_fresh_terminal(agent, variant, ws, state, event, host=host)
            state.log_run(event, action="created")
            print(f"dispatch[{agent}]: no terminal — created fresh -> {cmd.skill}")
            return 0

        survivor = max(terms, key=lambda pane: pane.last_output_at)
        if not _is_idle(survivor.handle, host=host):
            quiet = _quiet_seconds(survivor, time.time())
            if quiet <= WATCHDOG_SECONDS:  # a fresh, working agent — don't interrupt or pile on
                state.log_run(event, action="busy-skip")
                print(f"dispatch[{agent}]: agent busy ({int(quiet)}s silent) — left running, no dispatch")
                return 0
            # busy but silent too long -> stuck: sweep and restart, reaping the ghost the stop
            # just made right away rather than leaving it for the top of the next run. Bail
            # without creating if the stop can't be confirmed -- proceeding anyway risks a second
            # live session alongside a stuck one that never actually died (review B3).
            if not _stop_and_confirm(ws, state, host=host):
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
            cmd = _spawn_fresh_terminal(agent, variant, ws, state, event, host=host)
            state.log_run(event, action="watchdog-restart")
            print(f"dispatch[{agent}]: busy but stuck ({int(quiet)}s silent) — watchdog restart -> {cmd.skill}")
            return 0

        # idle: an ephemeral agent (curator, triggered-agents-445) never reuses a warm terminal —
        # the previous run just finished (successfully or not), so tear its terminal + tab down
        # and start the next tick on a brand new provider session, same shape as the watchdog
        # restart above minus the profile-red gate below (a fresh spawn always re-resolves the
        # head, so there's nothing to divert from).
        if _is_ephemeral(agent):
            if not _stop_and_confirm(ws, state, host=host):
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
            cmd = _spawn_fresh_terminal(agent, variant, ws, state, event, host=host)
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
            if not _stop_and_confirm(ws, state, host=host):
                state.log_run(event, action="red-fallback-stop-failed")
                print(f"dispatch[{agent}]: red-fallback stop could not confirm the idle terminal "
                      "stopped — leaving it for the next tick")
                return 0
            cmd = _spawn_fresh_terminal(agent, variant, ws, state, event, host=host)
            state.log_run(event, action="reused-red-fallback")
            print(f"dispatch[{agent}]: idle terminal's head is red — stopped, fresh fallback terminal -> {cmd.skill}")
            return 0

        # idle: a terminal can remain live after its agent exits, leaving bash in the same pane.
        # `tui-idle` reports that shell as idle too, so inspect the rendered panel before any
        # slash command is sent. A dead REPL takes the normal stop/reap/fresh-create route.
        # Its telemetry action is `warm-repl-restart`, not `reused`.
        if not _agent_repl_visible(survivor.handle, host=host):
            if not _stop_and_confirm(ws, state, host=host):
                state.log_run(event, action="warm-repl-stop-failed")
                print(f"dispatch[{agent}]: idle terminal has no live agent REPL, but its stop "
                      "could not be confirmed — leaving it for the next tick")
                return 0
            _, ok = _reap_ghosts(ws)
            if not ok:
                state.log_run(event, action="warm-repl-restart-tab-failed")
                print(f"dispatch[{agent}]: idle terminal had no live agent REPL; stopped it but "
                      "a ghost tab would not close, not restarting this tick")
                return 0
            cmd = _spawn_fresh_terminal(agent, variant, ws, state, event, host=host)
            state.log_run(event, action="warm-repl-restart")
            print(f"dispatch[{agent}]: idle terminal had no live agent REPL: fresh terminal -> "
                  f"{cmd.skill}")
            return 0

        # idle: warm reuse, killing nothing -> no ghost. Close only legacy duplicates (one-time).
        state.save_terminal_handle(survivor.handle)
        extras = [pane for pane in terms if pane.handle != survivor.handle]
        for pane in extras:
            _unchecked(lambda handle=pane.handle: host.close_pane(handle))
        _unchecked(lambda: host.send(survivor.handle, "/clear", enter=True))
        time.sleep(1.0)  # let /clear settle before the skill lands
        try:
            cmd = _send_reuse_dispatch(agent, variant, survivor.handle, ws, state, event, host=host)
        except TuiDeliveryError as exc:
            # Both shapes of unconfirmed delivery — a seeded head's own record never appearing and
            # the interactive path never proving the prompt landed — are the same warm-reuse
            # failure to this tick, and are recorded as it.
            state.log_run(event, action="reuse-delivery-unconfirmed", result="error", error=str(exc))
            print(f"dispatch[{agent}]: warm-reuse delivery was not confirmed ({exc})", file=sys.stderr)
            raise
        state.log_run(event, action="reused")
        tail = f"; closed {len(extras)} dup(s)" if extras else ""
        print(f"dispatch[{agent}]: reused idle terminal (/clear -> {cmd.skill}){tail}")
        return 0
