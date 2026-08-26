"""Singleton terminal driver, shared by every triggered-agent.

Replaces `orca automations run` in the systemd trigger. One agent = one warm terminal in
its worktree, reused across ticks. On a trigger (after precheck passes, under the run lock):

  * no agent terminal          -> create one running the agent's resolved head profile
  * one idle agent terminal    -> `/clear` it and re-send <skill> (warm reuse, kills nothing)
  * ...unless its head is red  -> stop it, start a fresh one on the resolved fallback instead
  * ...unless agent is ephemeral -> stop + tear down instead, start a fresh one
  * it's busy and fresh        -> leave it working, dispatch nothing
  * it's busy but stuck        -> watchdog: stop the workspace and start one fresh

Warm reuse rather than stop+create every run: Orca retains a dead pty as a ghost tab in the
workspace session after the process exits. Reuse never kills the process, so no ghost is born;
the kill paths do leave ghosts, so every run first reaps them via `session.tabs.close`
(`_reap_ghosts`) — the one lever that reaches the session store, which the `terminal` CLI cannot.

An agent whose automation.toml sets `ephemeral = true` opts out of warm reuse entirely, so a stale
session cannot carry a half-finished write from a prior tick into the next one's judgment. Both
kill paths reap their own ghost tab immediately, and `_create_terminal` appends a `; `-separated
launcher trailer that starts a detached `finalize()` helper (`dispatch --finalize`) on head exit,
so teardown does not wait for a future tick's poll. `run()`'s cleanup-only/watchdog/stray-sweep
paths remain the backstop for a terminal that never reaches its trailer at all.

"Busy vs idle" is Orca's tui-idle condition; "stuck" is busy with no output for WATCHDOG_SECONDS.
Orca's agent status is known to wedge on 'working' after a silent exit, so a bare busy check would
freeze the agent forever — the watchdog makes "skip when busy" safe.

Invariants this module holds:

  * Every fresh spawn prepares the workspace for its head's own runtime first, via
    `_ensure_head_ready`, before any pane is created: a head that lands on a first-run dialog hangs
    on stdin nobody sends and never renames its tab away from the shell default, so it is invisible
    to `_agent_terminals` and is neither reused nor reaped. Best-effort for `claude`; for an
    interactive `codex` head the directory-trust entry is a hard precondition that fails the spawn.
  * A live terminal never re-resolves its head profile, so every spawn that resolves one records it
    via `AgentState.save_head_profile` — the only place idle-reuse can learn which resource the warm
    terminal actually runs against, which may be a fallback rather than the static preferred head.
  * `run(..., cleanup_only=True)` never dispatches a skill. For an ephemeral agent it still runs
    `_cleanup_only`, because otherwise a finished ephemeral run sits until a tick with real work.
    It bails immediately for a non-ephemeral agent, before constructing `AgentState` or taking
    `state.lock()`, so a precheck skip stays a zero-side-effect no-op and cannot contend the lock.
  * Every "stop, then create" path verifies the stop through `_stop_and_confirm` — re-listing
    terminals rather than trusting `terminal stop`'s exit code — and bails without creating if it
    cannot confirm; otherwise a silently-failed stop yields two live sessions for one singleton.
  * The "no terminal" branch checks `AgentState.load_terminal_created_at()` against
    `CREATE_VISIBILITY_GRACE_S` first: a terminal this agent just created may not be visible in
    `terminal list` yet, and a second dispatch in that gap must not create a duplicate.
  * When `_agent_terminals` recognizes nothing, both that branch and `_cleanup_only` still check
    `_raw_terminal_count` — Orca's unfiltered list — and sweep before creating or declaring the
    workspace clean, because the title/handle filter can miss a genuinely live stray.
    `_stop_and_confirm_workspace_empty` verifies through the same unfiltered count, since the
    filtered view would "confirm" success on a stray it could never recognize either way.

Every *pane* this scheduler drives it drives through `SessionHost`. The pane verbs live in
`pane_host`; this module holds only which pane is the survivor, when idle means reuse and when it
means teardown, what a stuck terminal is, and how many kinds of failure a stop has. `_run_json`
runs the argument vectors the host hands it rather than building any.

A pane is not the only way a head is held. Which backend holds this agent's head is its resolved
profile's own answer (`runtime`, secretary-1467), turned into a backend through the product's one
name-to-backend mapping (`head_runtime_backends`). One resolution decides it, and `_tick` reads it
off that resolution before it builds anything: `_resolve_launch` names the head and the backend,
`_render_launch` turns it into a command, and only the second of those creates the steward's report
card. Everything a tick decides about who holds this role's head it therefore decides while it owes
no card at all. `_supervised_bring_up` still asks the same reader about the command it was handed,
because that is the verb `open_pane` sits under.

One reading of the registry answers both of a tick's questions about it. `run()` takes a
`RegistrySnapshot` before either backend is touched and hands it down; the cheap "could this agent
be supervised at all" walk and the resolution the command is rendered from read that one snapshot,
so they cannot land on either side of an ordinary profile publication and disagree. That is what
makes the cheap answer safe to act on: `False` is over the same registry and the same fallback
closure the resolution picks from, so nothing this tick resolves afterwards can name a supervisor,
and the pane lifecycle below it — the ghost reap, the pane inventory, the idle probe — is only ever
reached by a tick a pane really does hold. A profile with no key, and one naming
`orca-legacy`, is the whole lifecycle above and nothing about it changes. A profile naming
`local-pty` takes the supervised tick instead, and that path is deliberately none of the above:
it raises one head per tick under a supervisor of this product's own, hands it its skill across
that same boundary, and reads success and refusal from typed receipts. It never lists, probes,
reads or reaps a pane, because there is none — every one of those verbs exists to cope with the
ghost tab Orca leaves behind, and a supervisor leaves none. The head outlives the tick that
raised it; `AgentState.save_head_run` is what a later tick reaches it through, and handing that
record back to `start` is what makes a bring-up over a head that is still working a refusal
rather than a second head.

One role, one owner of its head, and the owner is written down here rather than looked for in
either backend's inventory: a pane is named by `terminal_handle.json` and a supervised head by
`head_run.json`, and at most one of the two ever stands. A tick whose resolved backend is not the
one the standing record names is a *handover* tick and not a bring-up: it closes the recorded owner
on that owner's own path — a pane by the pane teardown, a supervised head by `stop` across the
`HeadRuntime` boundary — forgets the record and dispatches nothing at all, neither a skill nor a
report card. It cannot dispatch either, because it is decided before a command is built: no card is
made in order to find out that this tick makes none. The head on the new backend is raised by the
next tick, when this role provably has no live head left, so two live heads for one role is not a
state any ordering of these ticks can reach. Both directions are fail-closed: an owner that cannot
be confirmed stopped leaves its record standing and this tick raises nothing.

A report card belongs to a head, so stopping the head is what closes it. The steward's report card
is created by the render of the skill that names it and is written by the head that render is
launched with; `active_report.json` is which card that is and which head has it. Both handovers
stop a head that may be holding one, so both close it as part of the stop, before the record
naming that head as this role's owner is forgotten — a card left in progress under a head this
driver has just ended is a sweep later steward reporting reads as still under way. That, and the
card a tick built but never handed to a head, are one tick-long obligation with one place that
discharges it: `_TickReports`, entered by `run()` around the whole tick, so every terminal path of
`_tick` — ordinary dispatch, busy-skip, both handovers, every fail-closed bail, every raise —
leaves through it.

A pane that answers `tui-idle` within `IDLE_PROBE_MS` is idle here and a probe that times out is
busy — two states, not the three the interactive delivery path classifies, because this scheduler
acts on "may I send into it" and not on why it may not. The calls whose outcome this module has
never checked (`/clear`, the warm-reuse send, the by-worktree stop, closing a legacy duplicate)
still ignore a refusal, and the ones that gate a spawn still raise.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import claude_env, finalizer, orca_rpc
from .claude_sessions import claude_session_paths
from .codex_preflight import (
    CodexPreflightError,
    preflight_codex_launch,
)
from .head import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_GONE,
    RUNTIME_ROLE_ENV,
    HeadRun,
    HeadRunError,
    HeadSpec,
    NudgePointer,
    StopInitiator,
    TaskRef,
    new_run_id,
    render_head_command,
)
from .head.identity import head_process_status
from .head_runtime_backends import build_head_runtime, head_runtime_name
from .head_runtimes import DEFAULT_HEAD_RUNTIME, LOCAL_PTY_RUNTIME
from .pane_host import Pane, SessionHost, safe_command_label, session_host
from .production_telemetry import data_dir as _installation_data_dir
from .state import AgentState
from .tui_delivery import (
    TuiDeliveryError,
    deliver_interactive_prompt,
    read_pane_text,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
CLAUDE_JSON = Path(os.environ.get("TA_CLAUDE_JSON", str(Path.home() / ".claude.json")))
WATCHDOG_SECONDS = int(os.environ.get("TA_WATCHDOG_SECONDS", "1200"))  # busy + this quiet = stuck
IDLE_PROBE_MS = 2500  # tui-idle satisfied within this = idle; timeout = busy
ORCA_TIMEOUT_S = 20  # never let a hung orca call wedge dispatch while it holds the lock
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
    """A warm terminal did not visibly accept its next skill command."""


def _run_json(args: list[str]) -> dict:
    """Run one of `pane_host`'s argument vectors and hand back Orca's result payload."""
    p = subprocess.run(args, capture_output=True, text=True, timeout=ORCA_TIMEOUT_S)
    if p.returncode != 0:
        raise RuntimeError(f"{safe_command_label(args)} failed: {(p.stderr or p.stdout).strip()}")
    data = json.loads(p.stdout)
    return data.get("result", data)


def _unchecked(work: Callable[[], Any]) -> None:
    """Perform a host call this scheduler has never looked at the outcome of.

    The `/clear` before a warm reuse, the warm-reuse send, the by-worktree stop (proven by
    re-listing, never by its own exit code) and the close of a legacy duplicate. A refusal and an
    unreadable answer are both ignored; a call that hangs is not, so `_run_json`'s timeout still
    reaches the caller and a wedged Orca fails the tick instead of being waited on inside the lock.
    """
    try:
        work()
    except (RuntimeError, ValueError):
        return


def _terminal_screen(handle: str, *, host: SessionHost) -> str:
    """Rendered terminal text, or an empty string when Orca cannot provide it.

    Reads the panel rather than inferring liveness from the terminal record: a completed agent
    leaves a perfectly live PTY behind, now owned by bash. The 200-line window is this caller's —
    a whole retained scrollback would let a marker from a session that ended hours ago answer for
    the pane as it is now.
    """
    return read_pane_text(handle, host=host, limit=_SCREEN_READ_LINES)


def _agent_repl_visible(handle: str, *, host: SessionHost) -> bool:
    """Whether the observed panel is an agent REPL, not the shell it may have returned to.

    A positive REPL marker is required as well as the absence of a shell prompt at the bottom of
    the panel: unknown or unreadable screens are unsafe to receive a slash command and take the
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
    """Yield Claude session logs for one workspace without scanning other projects."""
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
    """Whether the Codex session for this workspace wrote anything after ``since``."""
    try:
        from ..agents.pipeline import codex_sessions

        latest = codex_sessions.latest_activity_for(workspace)
    except Exception:
        return False
    return latest is not None and latest > since


def _confirm_delivery(handle: str, workspace: str, sent_at: float, *, host: SessionHost) -> None:
    """Wait until the head durably records the command just sent into its live session."""
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
        f"delivery was not confirmed after {REUSE_DELIVERY_TIMEOUT_S:.1f}s (reason={last_reason})"
    )


def _workspace(agent: str) -> str:
    return os.environ.get("TA_WORKSPACE") or str(Path.home() / "orca/workspaces/secretary" / agent)


def _load_spec(agent: str) -> dict:
    return tomllib.loads(
        (_REPO_ROOT / "src" / "triggered_agents" / "agents" / agent / "automation.toml").read_text()
    )


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
            f"dispatch: pipeline pause state is unreadable; refusing dispatch ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return True


@dataclass(frozen=True)
class RegistrySnapshot:
    """The head registry as one tick read it, once.

    A tick asks the registry two questions — "could this agent land on a head this product
    supervises" and, later, "which profile did this launch resolve to and what runtime does it
    name" — and an ordinary profile publication is atomic but not instantaneous relative to a
    scheduled tick. Asking twice let the two answers disagree, and the tick then acted on the
    first while the command it built came from the second. So the reading is taken once, before
    the first verb of either backend, and every question of this tick is answered from it.

    `registry` is `None` when the registry could not be read at all. That is the pane backend and
    a launch on the bare default-model `claude`, which is the fail-safe this driver has always
    had for an unreadable registry — reached here without a second attempt to open it.
    """

    registry: Any | None


def _registry_snapshot() -> RegistrySnapshot:
    """This tick's one reading of the head registry.

    Cheap by construction: parsing the registry file probes no resource, so a tick that will exit
    early on the pane path pays exactly what its first of the former readings already cost. A
    caller outside a tick (a test, a one-shot helper) passes no snapshot and gets one of its own.
    """
    try:
        from ..agents.pipeline import heads as pipeline_heads

        return RegistrySnapshot(pipeline_heads.load_registry())
    except Exception:
        return RegistrySnapshot(None)


def _preferred_head(agent: str, spec: dict, snapshot: RegistrySnapshot | None = None) -> str | None:
    """The head this agent launches on: the selected registry's role default for it.

    The spec's own `head` is the last resort for a registry that routes this role nowhere, and it
    goes through the registry's own resolution. A resolution refusal reaches the caller rather than
    becoming a bare `claude` invocation: a Codex-pinned service agent whose registry has no
    interactive Codex head left for that name is a dispatch that must not happen.

    `snapshot` is the tick's one reading of the registry (`RegistrySnapshot`), so this answer and
    the resolution's cannot come from two different registries.
    """
    registry = (_registry_snapshot() if snapshot is None else snapshot).registry
    if registry is None:
        return spec.get("head")
    routed = registry.role_default(agent)
    if routed:
        return routed
    fallback = spec.get("head")
    return registry.resolve(fallback) if fallback else fallback


def _reuse_head_is_red(agent: str, state: AgentState, snapshot: RegistrySnapshot | None = None) -> bool:
    """Whether the profile the idle terminal was ACTUALLY launched with is currently sitting on a
    red resource — the check idle-reuse needs before sending into an already-warm terminal, since
    that terminal keeps whatever profile it was spawned with and never re-resolves on its own
    (only a fresh spawn does, via `_launch_cmd`).

    Answered from this tick's one reading of the registry. A reading that failed is not retried
    here: an unreadable registry has always left the warm terminal alone (the probe below raises
    on it and the answer is `False`), and asking again would be the second reading this tick is
    built to do without.
    """
    snapshot = _registry_snapshot() if snapshot is None else snapshot
    if snapshot.registry is None:
        return False
    try:
        head = _preferred_head(agent, _load_spec(agent), snapshot)
        if not head:
            return False
        profile = state.load_head_profile() or head
        from ..agents.pipeline import health as pipeline_health

        statuses = pipeline_health.refresh(snapshot.registry)
        resource = pipeline_health.resource_of(profile, snapshot.registry)
        return resource is not None and statuses.get(resource, pipeline_health.GREEN) == pipeline_health.RED
    except Exception:
        return False


@dataclass(frozen=True)
class LaunchResolution:
    """Which head this agent gets this tick, and which backend holds it.

    A resolution, not a dispatch, and it exists before any report card does. That order is the
    point of having it at all: the card is created by the very render that names it in the skill,
    so a tick that has to decide something *about* the head — which backend holds it, whether this
    tick is a handover between backends — must be able to decide it without rendering anything, or
    the deciding itself files a report nobody is ever going to write.
    """

    #: The role's skill text as its spec has it, with no `--card` argument yet.
    skill: str
    #: The profile this launch resolved onto, and its data as the registry gave it. Both None for
    #: the bare default-model `claude` invocation an agent no registry routes still gets.
    profile: str | None
    head_profile: dict | None
    #: Which backend holds a head raised from that profile, read the product's one way.
    runtime: str


def _resolve_launch(
    agent: str, variant: str | None = None, snapshot: RegistrySnapshot | None = None
) -> LaunchResolution:
    """Resolve this agent's head against this run's live resource health.

    The head comes from `_preferred_head` and resolves through the same registry machinery a
    worker or reviewer head gets. Any resolution failure falls back to the bare default-model
    `claude` invocation rather than leaving the agent undispatched for the whole tick.

    `variant` reads `skill` from `spec["variants"][variant]` instead of the top-level one.

    `snapshot` is the tick's one reading of the registry: the profile this resolution lands on,
    and the runtime that profile names, come out of the same registry the tick's earlier question
    about a supervisor was answered from.
    """
    snapshot = _registry_snapshot() if snapshot is None else snapshot
    spec = _load_spec(agent)
    skill = spec["variants"][variant]["skill"] if variant else spec["skill"]
    head = _preferred_head(agent, spec, snapshot)
    registry = snapshot.registry
    if not head or registry is None:
        return LaunchResolution(skill, None, None, DEFAULT_HEAD_RUNTIME)
    try:
        from ..agents.pipeline import health as pipeline_health

        statuses = pipeline_health.refresh(registry)
        resolved = pipeline_health.resolve_head(head, statuses, registry) or head
        profile = registry.profile(resolved)
    except Exception:
        return LaunchResolution(skill, None, None, DEFAULT_HEAD_RUNTIME)
    return LaunchResolution(skill, resolved, profile, _profile_runtime(resolved, profile))


def _render_launch(
    agent: str,
    resolution: LaunchResolution,
    card_ref: str | None = None,
) -> tuple[str, str, bool, LaunchResolution]:
    """(skill, launch command, prompt-after-start, the resolution the command was rendered from).

    `card_ref` appends `--card <ref>` to the skill text BEFORE it is handed to the head, so the
    augmented text is what actually gets sent rather than landing outside the quoted prompt. It is
    the whole reason rendering is a separate step from resolving: a card is younger than the head
    it names, and every question this tick asks about the head is older than the card.

    A resolution with no profile — and one the renderer will not take — is the bare invocation,
    and the resolution handed back says so: a launch that is not this profile's must not be
    recorded as that profile's either. Both are rendered by the same renderer from a profile (the
    fallback's profile is just the emptiest one there is), so a background agent's command cannot
    drift from a pipeline head's by being assembled somewhere else.
    """
    skill = f"{resolution.skill} --card {card_ref}" if card_ref else resolution.skill
    bare = LaunchResolution(resolution.skill, None, None, DEFAULT_HEAD_RUNTIME)
    bare_claude = render_head_command(
        {"adapter": "claude"},
        prompt=skill,
        role=agent,
        binding=RUNTIME_ROLE_ENV,
    ).command
    if resolution.head_profile is None:
        return skill, bare_claude, False, bare
    try:
        rendered = render_head_command(
            resolution.head_profile,
            prompt=skill,
            role=agent,
            workspace=_workspace(agent),
            binding=RUNTIME_ROLE_ENV,
        )
    except Exception:
        return skill, bare_claude, False, bare
    return skill, rendered.command, rendered.prompt_after_start, resolution


def _launch_cmd(
    agent: str,
    variant: str | None = None,
    card_ref: str | None = None,
    snapshot: RegistrySnapshot | None = None,
) -> tuple[str, str, str | None, bool, dict | None]:
    """(skill, full launch command, resolved head profile, prompt-after-start, profile data) from
    the agent's automation.toml: `_resolve_launch` and then `_render_launch`, for a caller that
    wants both halves at once.

    The caller records the third element via `AgentState.save_head_profile`, so a later idle-reuse
    tick can check the resource this very terminal runs against rather than the agent's static
    preferred head.

    The fifth element is the resolved profile's own data: an interactive head has its workspace
    prepared before its pane exists, and the preflight reads CODEX_HOME from the profile the
    command was rendered from. Resolving it a second time at the call site could answer differently
    and write trust into a home the head never reads.
    """
    resolution = _resolve_launch(agent, variant, snapshot)
    skill, launch, after_start, used = _render_launch(agent, resolution, card_ref)
    return skill, launch, used.profile, after_start, used.head_profile


def _steward_report_card(agent: str, variant: str | None) -> str | None:
    """Create the steward's own wake-up report card (project secretary, non-code type,
    straight into In progress, already claimed by itself — see pipeline.ops.create_report_card)
    right before a dispatch actually reaches the head. None for every agent but steward
    (triggered-agents-255): the rest keep their existing dispatch untouched.
    """
    if agent != "steward":
        return None
    from ..agents.pipeline import ops as pipeline_ops

    now = datetime.now(UTC)
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
    `ephemeral` field or a missing automation.toml (e.g. a test's synthetic agent name) retain
    the existing warm-reuse behavior. A spec that exists but cannot be read is fail-closed: it
    never grants a warm session that may retain curator material across ticks.
    """
    try:
        return bool(_load_spec(agent).get("ephemeral"))
    except FileNotFoundError:
        return False
    except Exception:
        return True


def _dispatch_command(
    agent: str,
    variant: str | None,
    snapshot: RegistrySnapshot | None = None,
    resolution: LaunchResolution | None = None,
) -> DispatchCommand:
    """(skill, launch, resolved head profile) for a dispatch about to actually reach the head —
    the one spot that also creates the steward's report card, so every real dispatch (fresh
    create, watchdog restart, idle reuse) carries one and a tick that dispatches nothing never
    does (no card, nobody to close it).

    `resolution` is the reading this tick already took of which head it is dispatching and which
    backend holds it; it is resolved here only for a caller that never needed one. Building the
    command is therefore the moment a card is created and never the moment a backend is chosen:
    everything a tick decides about who holds this role's head — including that this tick is a
    handover between backends and dispatches nothing at all — is decided on the resolution, before
    a card exists to be left behind.

    `snapshot` is the tick's one reading of the registry, carried through to the resolution."""
    resolution = _resolve_launch(agent, variant, snapshot) if resolution is None else resolution
    card_ref = _steward_report_card(agent, variant)
    skill, launch, after_start, used = _render_launch(agent, resolution, card_ref)
    return DispatchCommand(
        skill, launch, used.profile, card_ref, prompt_after_start=after_start, head_profile=used.head_profile
    )


def _terminal_handle_live(ws: str, handle: str, *, host: SessionHost) -> bool:
    try:
        panes = host.panes(ws)
    except Exception:
        return False
    return any(pane.handle == handle for pane in panes)


def _fresh_steward_report_in_progress(
    agent: str, now: float, ws: str, state: AgentState, *, host: SessionHost
) -> dict | None:
    """A secondary run guard for steward dispatch.

    Orca terminal creation is not immediately visible in `terminal list` on every host, so two
    timers firing close together can create a second report card and head. The report card is
    already the durable "this run exists" marker, so it short-circuits while it is younger than the
    steward stale threshold; once stale, a later run is allowed through to close or escalate it.
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
            if _terminal_handle_live(ws, handle, host=host) or now - moved < REPORT_VISIBILITY_GAP_SECONDS:
                return card
            state.clear_active_report(card.get("reference"))
    except Exception:
        return None
    return None


def _ensure_head_ready(ws: str, cmd: DispatchCommand, *, role: str = "service") -> None:
    """Prepare `ws` for the head about to be spawned into it, on that head's own runtime.

    The first-run question a runtime asks is the one thing that can make a fresh pane never come up
    at all, and which question it is depends on the runtime, so this is the one place that branches
    on it, right before `_create_terminal`. The two failure modes differ deliberately: Claude's
    preparation stays best-effort, while the Codex preflight is a hard precondition — without the
    trust entry the pane cannot reach readiness — and it raises before any pane is created.
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

    Without this a head can land on an interactive prompt, wait forever for input nobody sends, and
    never rename its terminal tab away from the shell default — invisible to `_agent_terminals`'s
    title match, so it is reused by nothing and reaped by nothing. Best-effort.
    """
    try:
        claude_env.ensure_trust(CLAUDE_JSON, ws)
        claude_env.ensure_theme(CLAUDE_JSON)
    except claude_env.ClaudeConfigError as e:
        print(f"dispatch: claude config prep failed ({e})")


def _agent_terminals(ws: str, state: AgentState | None = None, *, host: SessionHost) -> list[Pane] | None:
    """Live terminals in the workspace running this singleton agent.

    New spawns get an explicit `triggered-agent:<name>` title. The legacy `Claude` match keeps
    already-warm Claude terminals reusable until they are naturally restarted. Codex may rename its
    tab back to the shell cwd after startup, so the latest saved Orca handle is also accepted.
    """
    try:
        panes = host.panes(ws)
    except Exception as exc:
        # An unreadable list is not an empty workspace. Every caller treats None as a deferred
        # decision so an Orca hiccup cannot turn into a second curator or a healthy teardown.
        print(f"dispatch: terminal list unavailable for {ws} ({exc})")
        return None
    saved_handle = state.load_terminal_handle() if state else None
    return [
        pane
        for pane in panes
        if (saved_handle and pane.handle == saved_handle)
        or pane.title.startswith("triggered-agent:")
        or "Claude" in pane.title
    ]


def _raw_terminal_count(ws: str, *, host: SessionHost) -> int | None:
    """Every live terminal Orca reports for `ws`, unfiltered by title/handle.

    Unlike `_agent_terminals` this also counts a stray the recognition filter would miss entirely —
    an orphan stuck on the shell's default title, or one predating the `triggered-agent:<name>`
    convention — so the stray-sweep paths converge to zero instead of creating a new terminal
    alongside one they cannot see.

    Returns None — "unknown", NOT zero — when the list call fails, times out or cannot be parsed. A
    confirmation path must never read an Orca hiccup as "confirmed empty", and a pre-create check
    must not create on top of a workspace whose real contents it could not read. Every caller
    distinguishes the three cases (0 / >0 / None) explicitly.
    """
    try:
        return len(host.panes(ws))
    except (RuntimeError, subprocess.TimeoutExpired):
        return None


_with_finalizer = finalizer.with_finalizer
spawn_finalizer = finalizer.spawn_finalizer
finalize = finalizer.finalize


def _create_terminal(
    agent: str, ws: str, launch: str, state: AgentState, profile: str | None, *, host: SessionHost
) -> str:
    """Open this agent's pane and record what was opened.

    `open_pane` refuses a create Orca answered without a handle, so a spawn that could not be
    addressed fails here rather than being recorded under a null handle and then delivered to.
    """
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
    state.save_terminal_handle(pane.handle, created_at=time.time(), generation=generation)
    state.save_head_profile(profile)
    return pane.handle


def _recover_steward_dispatch_failure(
    state: AgentState, event: str, cmd: DispatchCommand, failure: BaseException
) -> None:
    """Close out a steward report card whose head was brought up but never took the run."""
    if not cmd.card_ref:
        return
    state.clear_active_report(cmd.card_ref)
    body = f"steward dispatch failed before the head accepted the report-card run.\n\nfailure: {failure}"
    try:
        from ..agents.pipeline import ops as pipeline_ops

        pipeline_ops.move_card("steward", cmd.card_ref, "Done", reason=body)
        state.log_run(event, action="dispatch-recovery", result="done", reference=cmd.card_ref)
    except Exception as recovery_error:
        state.log_run(
            event,
            action="dispatch-recovery",
            result="failed",
            reference=cmd.card_ref,
            error=str(recovery_error),
        )


def _release_steward_report(state: AgentState, event: str, cmd: DispatchCommand, note: str) -> None:
    """Close a steward report card whose tick turned out to dispatch nothing after all.

    The card is created by the same call that renders the skill naming it, so a tick cannot know
    it will refuse until it holds one. Leaving it open would park a report in progress that no head
    is ever going to write, so it is closed with the reason it went unused.
    """
    if not cmd.card_ref:
        return
    state.clear_active_report(cmd.card_ref)
    try:
        from ..agents.pipeline import ops as pipeline_ops

        pipeline_ops.move_card("steward", cmd.card_ref, "Done", reason=note)
        state.log_run(event, action="dispatch-release", result="done", reference=cmd.card_ref)
    except Exception as error:
        state.log_run(
            event, action="dispatch-release", result="failed", reference=cmd.card_ref, error=str(error)
        )


def _escalate_steward_preflight_failure(
    state: AgentState, event: str, cmd: DispatchCommand, failure: BaseException
) -> None:
    """Put a steward report card in front of a human when its workspace could not be prepared.

    The preflight fails before any pane is created, so no head has seen this card. Closing it as
    Done would record a sweep that never ran, and the condition — an untrusted repository root, a
    codex config the launcher may not rewrite — does not heal on its own, so the card goes to
    Blocked with the preflight's reason. A card that cannot even be moved leaves its reason in the
    run log rather than raising over the real cause.
    """
    if not cmd.card_ref:
        return
    state.clear_active_report(cmd.card_ref)
    body = (
        "steward dispatch could not prepare the head workspace, so no head was started "
        "and no sweep ran.\n\n"
        f"failure: {failure}"
    )
    try:
        from ..agents.pipeline import ops as pipeline_ops

        pipeline_ops.move_card("steward", cmd.card_ref, "Blocked", reason=body)
        state.log_run(
            event, action="dispatch-preflight", result="blocked", reference=cmd.card_ref, error=str(failure)
        )
    except Exception as escalation_error:
        state.log_run(
            event,
            action="dispatch-preflight",
            result="failed",
            reference=cmd.card_ref,
            error=f"{failure} (escalation failed: {escalation_error})",
        )


def _release_standing_report(state: AgentState, event: str, note: str) -> None:
    """Close the steward report card the head this tick has just stopped was writing.

    The card in `active_report.json` belongs to the head recorded as this role's owner, not to
    the tick that finds it: whoever ends that head inherits its report. Nobody is going to write
    it now, so it is closed with the reason its writer was stopped — and closed here, while the
    record naming that writer still stands, because a record forgotten first leaves a card in
    progress that no later tick can even tell was orphaned.

    Nothing for a role with no reporting contract: only a steward dispatch ever records a
    reference here, so for curator and retro this reads an empty record and returns.
    """
    reference = (state.load_active_report() or {}).get("reference")
    if not reference:
        return
    state.clear_active_report(reference)
    try:
        from ..agents.pipeline import ops as pipeline_ops

        pipeline_ops.move_card("steward", reference, "Done", reason=note)
        state.log_run(event, action="owner-report-release", result="done", reference=reference)
    except Exception as error:
        state.log_run(
            event, action="owner-report-release", result="failed", reference=reference, error=str(error)
        )


class _TickReports:
    """Everything one tick owes a steward report card, and the one place that discharges it.

    A tick owes two cards, and neither of them used to have a single owner in this module. One is
    its own: `_dispatch_command` creates a report card as it renders the skill that names it, so a
    tick that turns out to dispatch nothing after all is holding a report nobody will ever write.
    The other was already standing when the tick began — the card in `active_report.json`, being
    written by the head this role already had — and a tick that stops that head inherits it.

    Both are discharged through this object, and `run()` enters it once around the whole tick, so
    every terminal path of the tick leaves through `__exit__`: the ordinary dispatches, the
    busy-skip, both backend handovers, every fail-closed bail and every raise. The question is
    never "did this branch remember" but "what is still outstanding", which is the only form of it
    a branch cannot get wrong by being added later.

    A tick builds at most one command, so at most one card is ever this tick's own. Handing it to
    a head (`taken`) is what settles it; so is closing it (`undispatched`), recording a dispatch
    that failed after the head was up (`failed`), and escalating a workspace that could not hold a
    head at all (`preflight_failed`). Anything still unsettled when the tick ends is closed here.
    """

    NOTHING_DISPATCHED = (
        "this tick dispatched nothing after all, so the report card it made is closed unwritten."
    )

    def __init__(self, agent: str, state: AgentState, event: str) -> None:
        self.agent = agent
        self.state = state
        self.event = event
        #: This tick's own card, and whether it has been discharged. Nothing is outstanding until
        #: a command carrying one exists.
        self.cmd: DispatchCommand | None = None
        self.settled = True

    def __enter__(self) -> _TickReports:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.cmd is not None and not self.settled:
            if isinstance(exc, CodexPreflightError):
                self.preflight_failed(self.cmd, exc)
            elif exc is not None:
                self.failed(self.cmd, exc)
            else:
                self.undispatched(self.cmd, self.NOTHING_DISPATCHED)
        self._close_an_orphan()
        return False

    def command(
        self,
        variant: str | None,
        snapshot: RegistrySnapshot | None = None,
        resolution: LaunchResolution | None = None,
    ) -> DispatchCommand:
        """This tick's dispatch command, and the card it carries, recorded as outstanding."""
        cmd = _dispatch_command(self.agent, variant, snapshot, resolution)
        self.cmd = cmd
        self.settled = cmd.card_ref is None
        return cmd

    def taken(self, cmd: DispatchCommand, handle: str | None) -> None:
        """A head has this card now: it is that head's to write, and the record says whose."""
        self.state.save_active_report(cmd.card_ref, handle)
        self.settled = True

    def undispatched(self, cmd: DispatchCommand, note: str) -> None:
        _release_steward_report(self.state, self.event, cmd, note)
        self.settled = True

    def failed(self, cmd: DispatchCommand, failure: BaseException) -> None:
        _recover_steward_dispatch_failure(self.state, self.event, cmd, failure)
        self.settled = True

    def preflight_failed(self, cmd: DispatchCommand, failure: BaseException) -> None:
        _escalate_steward_preflight_failure(self.state, self.event, cmd, failure)
        self.settled = True

    def owner_stopped(self, note: str) -> None:
        """The head that owned the standing report has just been stopped by this tick.

        Called by a handover between the moment its stop is confirmed and the moment the record
        naming that head is forgotten, which is the only order in which the card can still be
        matched to the head that was writing it.
        """
        _release_standing_report(self.state, self.event, note)

    def _close_an_orphan(self) -> None:
        """The backstop under both of the above: a report with no owner left anywhere.

        `active_report.json` names a card and its writer at once, and this driver records the
        writer of a live head in exactly two places — `terminal_handle.json` for a pane and
        `head_run.json` for a supervised head. A tick that ends with a report standing and neither
        record standing has removed that card's writer without closing it, whatever branch did so,
        and the card is closed here rather than left in progress for a later tick to overwrite.
        """
        try:
            if self.state.load_active_report() is None:
                return
            if self.state.load_terminal_handle() is not None:
                return
            if self.state.load_head_run() is not None:
                return
        except Exception:
            return
        _release_standing_report(
            self.state,
            self.event,
            "the head that was writing this report is no longer this role's recorded head, so "
            "the report was closed unwritten by the tick that found it ownerless.",
        )


def _deliver_interactive_skill(handle: str, workspace: str, skill: str, *, host: SessionHost) -> None:
    """Put a service head's skill in front of it, on the product's one interactive delivery path.

    A Codex head starts with an empty composer, fresh or warm, so the dispatch is not finished when
    `terminal create` (or `/clear`) returns. The confirmation criterion stays this side's: Codex
    having durably recorded the turn for this workspace after the send boundary. A failure raises
    and leaves the terminal up and idle, so the next tick re-sends through the reuse path rather
    than piling a second head beside a silent one.
    """
    deliver_interactive_prompt(
        handle,
        skill,
        host=host,
        adapter="codex",
        confirm=lambda sent_at: _codex_turn_after(workspace, sent_at),
    )


def _spawn_fresh_terminal(
    agent: str,
    variant: str | None,
    ws: str,
    state: AgentState,
    event: str,
    *,
    host: SessionHost,
    cmd: DispatchCommand | None = None,
    snapshot: RegistrySnapshot | None = None,
    resolution: LaunchResolution | None = None,
    reports: _TickReports | None = None,
) -> DispatchCommand | int:
    """Bring a fresh head up in `ws`: prepare the workspace, create the pane, deliver the skill.

    The preparation is deliberately outside the recovery below: once a pane exists the head may
    have started work, so a failure after that point is a run that has to be closed out, while a
    failure before it started nothing at all.

    `cmd` is the dispatch this tick has already built — the diverted-launch case in `run()`, and
    nothing else. Reusing it is what keeps that tick to one resolution and one report card, and
    `resolution` does the same for a tick that has read which head it is dispatching but has not
    yet had a reason to name a card.

    `reports` is the tick's one place for what it owes that card. A caller outside a tick gets an
    object of its own, which is the same behaviour reached the same way.

    Returns this tick's exit status instead of a command when the resolution it is holding names a
    supervisor. A tick answers both its registry questions from one snapshot, so a pane tick that
    reaches here cannot be holding a supervised command; the bring-up is still asked before
    `_create_terminal`, which is where `open_pane` is, so the invariant is stated at the verb it
    protects rather than inferred from the caller that got here.
    """
    reports = _TickReports(agent, state, event) if reports is None else reports
    cmd = reports.command(variant, snapshot, resolution) if cmd is None else cmd
    supervised = _supervised_bring_up(agent, ws, state, event, cmd, host=host, reports=reports)
    if supervised is not None:
        return supervised
    try:
        _ensure_head_ready(ws, cmd, role=agent)
    except CodexPreflightError as exc:
        reports.preflight_failed(cmd, exc)
        raise
    try:
        handle = _create_terminal(agent, ws, cmd.launch, state, cmd.profile, host=host)
        if cmd.prompt_after_start:
            _deliver_interactive_skill(handle, ws, cmd.skill, host=host)
    except Exception as exc:
        reports.failed(cmd, exc)
        raise
    reports.taken(cmd, handle)
    return cmd


def _send_reuse_dispatch(
    agent: str,
    variant: str | None,
    terminal_handle: str,
    workspace: str,
    state: AgentState,
    event: str,
    *,
    host: SessionHost,
    cmd: DispatchCommand | None = None,
    snapshot: RegistrySnapshot | None = None,
    resolution: LaunchResolution | None = None,
    reports: _TickReports | None = None,
) -> DispatchCommand:
    reports = _TickReports(agent, state, event) if reports is None else reports
    cmd = reports.command(variant, snapshot, resolution) if cmd is None else cmd
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
        reports.failed(cmd, exc)
        raise
    reports.taken(cmd, terminal_handle)
    return cmd


def _stop_and_confirm(ws: str, state: AgentState, *, host: SessionHost) -> bool:
    """Stop every live terminal in `ws` and verify the workspace actually went quiet.

    `terminal stop`'s own exit code is not trustworthy enough to gate a fresh spawn on — Orca can
    report success while a pty lingers — so the stop is issued through `_unchecked` and the
    workspace is re-listed through `_agent_terminals` instead. The stop is by worktree, not by pane:
    stopping the survivor alone would leave the others. A caller that gets False back must NOT
    proceed to `_create_terminal`, or one singleton agent ends up with two live sessions.

    Only correct for a terminal `_agent_terminals` actually recognized. For a stray it never
    recognized, use `_stop_and_confirm_workspace_empty`: the filtered view would "confirm" success
    on a stray it could not see either way.
    """
    try:
        _unchecked(lambda: host.stop_workspace(ws))
        time.sleep(1.0)
        terms = _agent_terminals(ws, state, host=host)
    except Exception as exc:
        print(f"dispatch: terminal stop/confirm failed for {ws} ({exc})")
        return False
    return terms == []  # None means the confirmation list was unavailable, not empty


def _stop_and_confirm_workspace_empty(ws: str, *, host: SessionHost) -> bool:
    """Stop every live terminal in `ws` and verify through Orca's UNFILTERED terminal list
    (`_raw_terminal_count`) that the workspace is truly empty — for the stray-sweep paths only.

    True only when the raw list came back AND was empty. A list failure (None, not 0) is not a
    confirmation: the caller must read it as "could not confirm the stop worked" and leave the
    terminal for the next tick.
    """
    try:
        _unchecked(lambda: host.stop_workspace(ws))
        time.sleep(1.0)
        return _raw_terminal_count(ws, host=host) == 0  # None (list failed) != 0 -> not confirmed
    except Exception as exc:
        print(f"dispatch: terminal stop/confirm failed for {ws} ({exc})")
        return False


def _is_idle(handle: str, *, host: SessionHost) -> bool:
    """Whether the pane will take input now, in the two states this scheduler acts on.

    The host's `tui-idle` wait with this module's `IDLE_PROBE_MS` budget: an answer that says
    satisfied is idle, and everything else — a refusal, a timeout, any other answer — is busy. A
    tick that cannot ask the question and a tick looking at a working head both leave the terminal
    alone, so the third state `tui_delivery.terminal_readiness` classifies has no branch here.
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

    `terminal list/stop/close` cannot touch these (they only reach live ptys); the persisted
    `tabsByWorktree` keeps them until `session.tabs.close` prunes them. Live tabs are status
    'ready'; a dead pty leaves 'pending-handle'.

    Returns `(closed, ok)`. `ok` is True ONLY when the listing succeeded AND every non-ready tab
    closed cleanly, so a teardown caller must not log a healthy self-teardown while it is False —
    that would claim "zero tabs after completion" over a ghost it never closed. The top-of-run
    opportunistic prune ignores `ok`; the real teardown paths gate their success action on it.
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
                    orca_rpc.call(
                        "session.tabs.close", {"worktree": snap["worktree"], "tabId": tab["parentTabId"]}
                    )
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


def _cleanup_only(
    agent: str, ws: str, state: AgentState, event: str, terms: list[Pane], *, host: SessionHost
) -> int:
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
            print(
                f"dispatch[{agent}]: cleanup — terminal list unavailable, cannot confirm the "
                "workspace is clear; leaving it for the next tick"
            )
            return 0
        if raw > 0:
            if not _stop_and_confirm_workspace_empty(ws, host=host):
                state.log_run(event, action="cleanup-stray-sweep-failed")
                print(
                    f"dispatch[{agent}]: cleanup could not confirm the workspace is clear of "
                    "stray terminals — leaving it for the next tick"
                )
                return 0
            reaped, ok = _reap_ghosts(ws)
            if not ok:
                state.log_run(event, action="cleanup-stray-swept-tab-failed")
                print(
                    f"dispatch[{agent}]: cleanup — stopped stray terminal(s) but a ghost tab "
                    "would not close; next tick re-reaps"
                )
                return 0
            state.log_run(event, action="cleanup-stray-swept")
            print(
                f"dispatch[{agent}]: cleanup — swept stray unrecognized terminal(s)"
                f"{f'; reaped {reaped} ghost(s)' if reaped else ''}, no new work to dispatch"
            )
        return 0
    survivor = max(terms, key=lambda pane: pane.last_output_at)
    if not _is_idle(survivor.handle, host=host):
        quiet = _quiet_seconds(survivor, time.time())
        if quiet <= WATCHDOG_SECONDS:
            return 0  # still working -- a no-work tick must never touch a live run
        if not _stop_and_confirm(ws, state, host=host):
            state.log_run(event, action="cleanup-stop-failed")
            print(
                f"dispatch[{agent}]: cleanup could not confirm the stuck terminal stopped "
                "— leaving it for the next tick"
            )
            return 0
        _, ok = _reap_ghosts(ws)
        if not ok:
            state.log_run(event, action="cleanup-watchdog-tab-failed")
            print(
                f"dispatch[{agent}]: cleanup — stopped stuck terminal but a ghost tab would not "
                "close; next tick re-reaps"
            )
            return 0
        state.log_run(event, action="cleanup-watchdog-stop")
        print(f"dispatch[{agent}]: cleanup — stopped stuck terminal, no new work to dispatch")
        return 0

    # idle: the previous run already finished; tear it down and leave the workspace empty, there
    # is no new work this tick to hand a fresh session to
    if not _stop_and_confirm(ws, state, host=host):
        state.log_run(event, action="cleanup-stop-failed")
        print(
            f"dispatch[{agent}]: cleanup could not confirm the finished terminal stopped "
            "— leaving it for the next tick"
        )
        return 0
    _, ok = _reap_ghosts(ws)
    if not ok:
        state.log_run(event, action="cleanup-teardown-tab-failed")
        print(
            f"dispatch[{agent}]: cleanup — stopped finished terminal but a ghost tab would not "
            "close; next tick re-reaps"
        )
        return 0
    state.log_run(event, action="cleanup-teardown")
    print(f"dispatch[{agent}]: cleanup — torn down finished terminal, no new work to dispatch")
    return 0


class LocalPtyDispatchError(RuntimeError):
    """A head this driver holds itself could not be brought up, as its own receipt said so."""


def _profile_runtime(profile_id: str | None, profile: dict | None) -> str:
    """Which backend a profile says its head is held by, read the one way the product reads it.

    Through `HeadSpec.from_profile`, which is where a `runtime` value is validated against the
    closed vocabulary, and then through the shared name reader the dispatcher's own lifecycle
    sites go through — so there is no second reading of the key here and no second list of names.
    A profile that will not make a spec, and the bare fallback invocation that has no profile at
    all, are heads this driver holds the way it has always held one: absence has meant
    `orca-legacy` since before the key existed, and an unusable profile is not a promotion.
    """
    if not profile:
        return DEFAULT_HEAD_RUNTIME
    try:
        return head_runtime_name(HeadSpec.from_profile(str(profile_id or "head"), dict(profile)))
    except Exception:
        return DEFAULT_HEAD_RUNTIME


def _may_be_supervised(agent: str, snapshot: RegistrySnapshot | None = None) -> bool:
    """Whether this tick could possibly end up on a head this product supervises itself.

    Not the decision. The decision is the resolved profile's own `runtime`, read off the very
    dictionary `_launch_cmd` rendered the command from — but that resolution probes every resource
    the registry names, and the tick has a question to answer strictly before it: its first acts
    are Orca's. Reaping ghost tabs and listing panes are questions about a session store a
    supervised head has no entry in, and a tick must not ask them about one.

    So this is the cheap half of the same snapshot: the routed profile and everything reachable
    from it through the fallback chain — the same set `health.resolve_head` chooses from, over the
    same registry the resolution will use — with no resource probed. That is what makes `False`
    a safe answer to act on: a resolution over this snapshot picks from this closure and nothing
    else, so no resolution of this tick can name a supervisor after it. The tick is then left
    exactly as it always was, down to the probes it does not make. `True` means one might, and the
    tick resolves once and decides on what it got.

    Fail-safe: an unreadable spec, an agent no registry routes and a registry that would not load
    are all the pane backend, which is the one this driver has always used.
    """
    snapshot = _registry_snapshot() if snapshot is None else snapshot
    registry = snapshot.registry
    if registry is None:
        return False
    try:
        routed = _preferred_head(agent, _load_spec(agent), snapshot)
        if not routed:
            return False
        seen: set[str] = set()
        queue = [routed]
        while queue:
            profile_id = queue.pop(0)
            if profile_id in seen:
                continue
            seen.add(profile_id)
            try:
                profile = registry.profile(profile_id)
            except Exception:
                continue
            if _profile_runtime(profile_id, profile) == LOCAL_PTY_RUNTIME:
                return True
            queue.extend(profile.get("fallback") or [])
        return False
    except Exception:
        return False


def _no_session() -> Any:
    """The session manager a supervised head never has.

    `build_head_runtime` is handed both dependencies because it is the one mapping for both
    backends. This driver only ever names the supervised one, and this is what says so out loud
    instead of opening a route to Orca that nothing on that path is allowed to use.
    """
    raise LocalPtyDispatchError("a supervised head is not held by a session manager")


def _local_pty_runtime() -> Any:
    """This driver's supervised backend, built through the product's one name-to-backend mapping.

    The run root is the installation's own `heads/` directory — the same one the Secretary
    dispatcher supervises its heads under — so a host keeps one place where a supervised head
    lives and one place an operator looks for it. The launch-identity reader is the product's
    single one, which is what lets a later tick ask whether the head an earlier tick started is
    still running.
    """
    return build_head_runtime(
        LOCAL_PTY_RUNTIME,
        session=_no_session,
        local_pty_root=lambda: _installation_data_dir() / "heads",
        head_process_status=head_process_status,
    )


#: Who ended a head this driver was holding when the role's profile started naming another
#: backend. A stop names its initiator, and this is the one this driver makes.
HANDOVER_INITIATOR = "triggered-agent-dispatch"
HANDOVER_REASON = "this role's resolved profile now names another backend"


def _hand_over_backend(
    agent: str,
    ws: str,
    state: AgentState,
    event: str,
    backend: str,
    reports: _TickReports,
    *,
    host: SessionHost,
) -> int | None:
    """This tick as a handover between backends, or `None` if it is an ordinary tick.

    One role, one owner of its head, and the owner is written down here rather than looked for in
    either backend's inventory: a pane is named by `terminal_handle.json` and a supervised head by
    `head_run.json`, and at most one of the two ever stands. A tick whose resolved backend is not
    the one the standing record names is a handover, and a handover dispatches nothing at all —
    not a skill and not a report card.

    Which is why it is decided here, before a command is built. The card is created by the render
    of the command, so a tick that built one first would file a report card in order to find out
    that it is not dispatching anything, close that one, and leave the card the stopped head was
    actually writing standing in progress with nobody left to write it.

    An integer means this tick was the handover and is over. `None` means the ordinary tick runs:
    the record either names this tick's own backend, or named a head that has provably ended and
    has been forgotten here.
    """
    if backend == LOCAL_PTY_RUNTIME:
        if state.load_terminal_handle() is None:
            return None
        return _hand_over_from_pane(agent, ws, state, event, reports, host=host)
    return _hand_back_supervised_head(agent, ws, state, event, reports)


def _hand_over_from_pane(
    agent: str, ws: str, state: AgentState, event: str, reports: _TickReports, *, host: SessionHost
) -> int:
    """The handover tick of a role whose head is a pane and whose profile now names a supervisor.

    Not a supervised tick, and the distinction is the whole point of having a separate name for it:
    nothing is raised through `HeadRuntime` here and nothing is dispatched. The tick closes the
    owner this driver wrote down — `terminal_handle.json` — on that owner's own path, which is the
    pane teardown and the pane confirmation this driver has always used, and then stops. The
    bring-up on the new backend is the next tick's, when this role provably has no live head left.
    A missed tick is the ordinary answer for a mechanical role: it has a watermark and a precheck.

    The report that head was writing is closed as part of stopping it, before the record naming it
    the owner is forgotten. It is the head that is being ended here, not the work it reported on:
    a sweep whose card stayed in progress with no live writer would be read by later steward
    reporting as one still under way.

    Nothing here inventories the other backend, in either direction. Which backend owns this role's
    head is read from this driver's own state, and a pane is asked about only because this driver
    wrote down that a pane is what it has.

    Fail-closed: a teardown this driver cannot confirm leaves the record standing and raises
    nothing, so the next tick is the handover again rather than a second head beside the first.
    """
    if not _stop_and_confirm(ws, state, host=host):
        state.log_run(
            event,
            action="handover-stop-failed",
            result="error",
            error="the pane holding this role's head could not be confirmed stopped",
        )
        print(
            f"dispatch[{agent}]: the pane holding this head would not confirm it stopped — "
            "not handing this role to a supervisor this tick"
        )
        return 0
    reaped, reap_ok = _reap_ghosts(ws)
    reports.owner_stopped(
        "the pane holding this role's head was stopped to hand the role to a supervisor of this "
        "product's own, so the report that head was writing is closed unwritten.",
    )
    state.save_terminal_handle(None)
    state.log_run(
        event,
        action="handover-to-supervised",
        result="done" if reap_ok else "partial",
        error="" if reap_ok else "a ghost tab of the stopped pane would not close",
    )
    tail = f"; reaped {reaped} ghost(s)" if reaped else ""
    print(
        f"dispatch[{agent}]: this role's profile now names a supervisor — stopped the pane that "
        f"held its head and dispatched nothing{tail}"
    )
    return 0


def _hand_back_supervised_head(
    agent: str, ws: str, state: AgentState, event: str, reports: _TickReports
) -> int | None:
    """The handover tick of a role whose head is supervised and whose profile now names a pane.

    `None` when there is nothing to hand back — no supervised head was ever written down for this
    role, or the one that was has provably ended — and the caller runs its ordinary pane tick. An
    integer means this tick was the handover and is over.

    The mirror of `_hand_over_from_pane`, and closed the same way in every respect: the owner is
    read from this driver's own `head_run.json`, it is stopped across the same `HeadRuntime`
    boundary that raised it, and the report it was writing is closed before the record naming it
    is forgotten. No pane is opened, no skill is delivered, no report card is created. The pane
    lifecycle below this point never runs beside a supervised head that is still alive, which is
    the singleton invariant this driver has always held, stated across both backends.

    Fail-closed in both of its uncertain answers, per this card's contract: a record that will not
    make a run, and a backend that cannot say what became of the head it names, both leave the
    record standing and raise nothing.
    """
    record = state.load_head_run()
    if record is None:
        return None
    try:
        run = HeadRun.from_json(record)
    except (HeadRunError, ValueError, TypeError) as exc:
        state.log_run(event, action="handover-owner-unreadable", result="error", error=str(exc))
        print(
            f"dispatch[{agent}]: the record of this role's supervised head does not name a run "
            f"({exc}) — raising nothing this tick"
        )
        return 0
    runtime = _local_pty_runtime()
    seen = runtime.observe(run)
    if seen.status == HEAD_GONE:
        # The head this role owned has ended on its own. There is no live owner to close, so this
        # is not a handover at all: the record is forgotten and the ordinary pane tick runs. The
        # report it may have been writing is nobody's to close from here — the head was not
        # stopped by this tick — and the tick's own end sees it has no owner left.
        state.save_head_run(None)
        state.log_run(event, action="handover-owner-gone", reference=run.run_id)
        return None
    if not seen.ok and seen.status != HEAD_ALIVE:
        state.log_run(
            event,
            action="handover-owner-unreadable",
            result="error",
            reference=run.run_id,
            error=seen.reason or seen.status,
        )
        print(
            f"dispatch[{agent}]: the backend holding this role's head cannot say whether it is "
            f"up ({seen.reason or seen.status}) — raising nothing this tick"
        )
        return 0
    receipt = runtime.stop(
        run,
        StopInitiator(actor=HANDOVER_INITIATOR, reason=HANDOVER_REASON),
    )
    if not receipt.ok:
        state.log_run(
            event,
            action="handover-stop-failed",
            result="error",
            reference=run.run_id,
            error=receipt.reason or receipt.status,
        )
        print(
            f"dispatch[{agent}]: the supervised head {run.run_id} would not confirm it stopped "
            "— not handing this role back to a pane this tick"
        )
        return 0
    reports.owner_stopped(
        "the supervised head of this role was stopped to hand the role back to a pane, so the "
        "report that head was writing is closed unwritten.",
    )
    state.save_head_run(None)
    state.log_run(event, action="handover-to-pane", reference=run.run_id)
    print(
        f"dispatch[{agent}]: this role's profile now names a pane — stopped the supervised head "
        f"{run.run_id} and dispatched nothing"
    )
    return 0


def _supervised_bring_up(
    agent: str,
    ws: str,
    state: AgentState,
    event: str,
    cmd: DispatchCommand,
    *,
    host: SessionHost,
    reports: _TickReports | None = None,
) -> int | None:
    """This tick under a supervisor of this product's own, or `None` if `cmd` is not held by one.

    The bring-up, and only the bring-up. Which backend holds this role's head, and whether this
    tick is a handover from the other one, is settled before any command exists — a card is
    created by the render of the command, and a tick that decides nothing is dispatched must be
    able to decide it without having made one. What is left here is the check at the verb it
    protects: no pane is ever opened for a command a supervisor holds, whichever branch of the
    tick built it, and `None` back is "the pane backend holds this one".

    The runtime is read off the very dictionary `_launch_cmd` rendered this command from, through
    the same reader the resolution above used, so the answer here cannot be a different one.

    Ephemeral by construction, and none of the Orca lifecycle is ported here. Warm reuse, `/clear`,
    ghost tabs, the `tui-idle` probe, the finalize trailer and the stray sweep all exist because
    Orca keeps a dead pty as a tab in its session store; a supervisor leaves no ghost behind, so a
    tick is one head raised with its own skill and the run ends when that head exits. Every outcome
    below is read from the backend's own typed receipt: no pane is created, listed, probed or read
    on this path, and no ghost is reaped.

    The head outlives the tick. What the next tick needs to reach it — its run id, workspace and
    spec — is written to this agent's state directory as the receipt recorded it, and handing that
    record back to `start` is what makes a second bring-up over a working head a refusal
    (`HEAD_BUSY`, `head_already_up`, made before anything is spawned) rather than a second head.
    """
    if _profile_runtime(cmd.profile, cmd.head_profile) != LOCAL_PTY_RUNTIME:
        return None
    reports = _TickReports(agent, state, event) if reports is None else reports
    if state.load_terminal_handle() is not None:
        # A pane is still recorded as this role's owner. The handover that ends it is decided
        # before a command is built, so reaching here means this bring-up was asked for by a
        # branch that never made that decision. Two live heads for one role is the one outcome
        # this driver may not produce, so the tick raises nothing and the handover is the next
        # tick's — the same fail-closed answer as a stop that would not confirm.
        reports.undispatched(
            cmd,
            "this role is still recorded as holding a pane, so no head was raised under a "
            "supervisor and this tick dispatched nothing.",
        )
        state.log_run(
            event,
            action="supervised-owner-conflict",
            result="error",
            error="a pane is still recorded as this role's head",
        )
        print(
            f"dispatch[{agent}]: a pane is still recorded as this role's head — raising no "
            "supervised head this tick"
        )
        return 0
    try:
        _ensure_head_ready(ws, cmd, role=agent)
    except CodexPreflightError as exc:
        reports.preflight_failed(cmd, exc)
        raise
    runtime = _local_pty_runtime()
    prior = state.load_head_run()
    spec = HeadSpec.from_profile(str(cmd.profile or agent), dict(cmd.head_profile or {}))
    # A standing duty, not a card. The run id and the task binding have to be the same facts every
    # tick, because they are what the head's own launch-identity record is compared against when a
    # later tick asks whether it is still up. The steward's report card is an argument of the
    # skill and travels in the prompt, exactly as it does on a pane.
    task_ref = TaskRef.standing(agent)
    run_id = str((prior or {}).get("run_id") or "") or new_run_id()
    run = HeadRun(run_id=run_id, spec=spec, workspace=ws, task_ref=task_ref, role=agent)
    # An adapter that takes its prompt on its command line is launched with it, as it is on a pane;
    # one that starts with an empty composer is pointed at its skill across the same boundary that
    # raised it. Neither shape touches a terminal API.
    pointer = NudgePointer.line(cmd.skill) if cmd.prompt_after_start else None
    receipt = runtime.start(
        spec,
        ws,
        task_ref,
        command=cmd.launch,
        title=f"triggered-agent:{agent}",
        pointer=pointer,
        run_id=run_id,
        role=agent,
        run=run,
        subject=f"{agent}-dispatch",
    )
    if receipt.status == HEAD_BUSY:
        # A head this role already has is still working. For a mechanical role a missed tick is the
        # normal answer to that — it has a watermark and a precheck — and the refusal is made
        # before anything is spawned, so no second head exists and no second skill was sent. The
        # report card this tick made is closed, because no head will ever write it; the report the
        # working head is writing is untouched, because that head is untouched.
        refusal = str((receipt.evidence or {}).get("refusal") or "busy")
        reports.undispatched(
            cmd,
            "the head of this role is still working, so this tick raised none and delivered "
            f"nothing.\n\nrefusal: {refusal}\n{receipt.reason}",
        )
        state.log_run(event, action="supervised-busy-skip", reference=cmd.card_ref, error=refusal)
        print(f"dispatch[{agent}]: head {run_id} is still up ({refusal}) — no dispatch")
        return 0
    if not receipt.ok:
        failure = LocalPtyDispatchError(receipt.reason or receipt.status)
        reports.failed(cmd, failure)
        raise failure
    live = receipt.run
    state.save_head_run(live.to_json())
    state.save_head_profile(cmd.profile)
    reports.taken(cmd, live.handle)
    state.log_run(event, action="supervised-started", reference=cmd.card_ref)
    print(f"dispatch[{agent}]: raised a supervised head {live.run_id} -> {cmd.skill}")
    return 0


def run(
    agent: str, variant: str | None = None, cleanup_only: bool = False, *, host: SessionHost | None = None
) -> int:
    """`variant` selects a differently-scheduled mode of the same agent: a different prompt from
    `_launch_cmd`, and its own runs.jsonl event name so the two wake-up kinds stay distinguishable
    in the agent's own telemetry.

    `cleanup_only` is `ta-gate.sh`'s call on a precheck skip: never dispatch a skill, but still let
    an ephemeral agent's finished or stuck terminal go through `_cleanup_only`.

    The tick itself is `_tick`, and this is what stands around it: the run lock, and the one place
    a tick's obligations to a steward report card are discharged. `_TickReports` is entered here
    and every way `_tick` can end — every return, every raise — leaves through it, so a report card
    this tick made and never handed to a head, and a report card whose head this tick stopped, are
    closed by the tick that owed them rather than by whichever branch happened to remember.
    """
    if cleanup_only and not _is_ephemeral(agent):
        # A non-ephemeral agent (retro/steward) has no terminal/PTY lifecycle for this pass to
        # clean up -- their warm-reuse lifecycle is out of scope for triggered-agents-445. Bail
        # BEFORE constructing AgentState or taking `state.lock()` (round 5 review B1), let alone
        # any pause check / ghost reap / `terminal list` / steward report lookup: ta-gate.sh calls
        # `--cleanup-only` on every precheck skip for EVERY agent, so acquiring the run lock here
        # would turn a quiet skip that used to print-and-exit-0 into `SystemExit: another run holds
        # the lock` the instant a deterministic helper is running (or a stale lock is left behind).
        # `_is_ephemeral` only reads automation.toml, not the lock/Orca/board. This keeps their
        # precheck skip the exact zero-side-effect no-op it always was before this card. Nothing
        # of this agent's is read or written here, so there is no card and no record to discharge.
        return 0
    ws = _workspace(agent)
    state = AgentState(agent)
    event = variant or "dispatch"
    # One session manager for the whole tick, resolved here and passed down: every pane this
    # tick lists, probes, sends into and stops is the same one, and a helper cannot quietly
    # open a second route to Orca of its own.
    host = session_host(_run_json) if host is None else host
    with state.lock(), _TickReports(agent, state, event) as reports:
        return _tick(agent, variant, ws, state, event, reports, cleanup_only=cleanup_only, host=host)


def _tick(
    agent: str,
    variant: str | None,
    ws: str,
    state: AgentState,
    event: str,
    reports: _TickReports,
    *,
    cleanup_only: bool,
    host: SessionHost,
) -> int:
    """One dispatch tick, under the run lock and inside `reports`.

    The order of the first three questions is the contract. The registry is read once; which
    backend holds this role's head is resolved out of that one reading; and only then, with the
    backend known and no report card in existence, is it decided whether this tick is a handover
    between backends or an ordinary tick. A command is built after all three, because building one
    creates the steward's report card: a tick that had to build one in order to discover it is
    dispatching nothing would file a report nobody writes, and would do it while the head that
    owns the standing report is being stopped underneath it.

    `_dispatch_command` therefore runs only in the branches that actually put the skill in front of
    a head — plus the one launch a health divert hands back to the pane lifecycle — and it is
    always reached through `reports`, which is what owes the card afterwards.
    """
    if _pipeline_paused():
        state.log_run(event, action="paused")
        print(f"dispatch[{agent}]: pipeline paused — no dispatch")
        return 0
    # One reading of the head registry for the whole tick, taken here: before it, the tick
    # has made no call of either backend, and after it every question about which head this
    # agent runs and which backend holds it is answered from this one reading. Two readings
    # are what let the cheap question and the resolution disagree across an ordinary profile
    # publication, and a tick that acted on the first while dispatching the second is the
    # defect this ordering removes rather than guards. Parsing the registry probes nothing,
    # so an early exit below pays no more than it did for the first of the readings this
    # replaces.
    registry = _registry_snapshot()
    # Which backend holds this agent's head, asked before a single Orca call is made. Every
    # question below this point — the ghost reap, the pane inventory, the idle probe — is about
    # a session store a supervised head has no entry in, so a tick must not ask them about one.
    # `False` here is final for this tick, because the resolution below reads the same
    # registry and picks from the same fallback closure this question walked, so nothing this
    # tick resolves afterwards can name a supervisor.
    resolution: LaunchResolution | None = None
    if _may_be_supervised(agent, registry):
        if cleanup_only:
            # `--cleanup-only` is the gate's call on a precheck skip, and its whole subject is a
            # pane a finished run left behind. There is none on this backend: a supervised head's
            # supervisor reaps its own process, and the durable record is what the next tick reads.
            state.log_run(event, action="supervised-cleanup-noop")
            return 0
        resolution = _resolve_launch(agent, variant, registry)
    # A resolution nobody took is the pane backend, which is the one this driver has always used.
    backend = resolution.runtime if resolution is not None else DEFAULT_HEAD_RUNTIME
    # One owner of this role's head at a time. A tick whose backend is not the one this driver
    # wrote the owner down under is the handover, and it ends here having dispatched nothing —
    # no skill, and no report card, because none has been built yet.
    handover = _hand_over_backend(agent, ws, state, event, backend, reports, host=host)
    if handover is not None:
        return handover
    # A `DispatchCommand` in `pending` means this agent could have landed on a supervised head but
    # this tick's own resolution landed on a pane profile: the ordinary tick runs from here, with
    # the command that resolution already produced rather than a second one.
    pending: DispatchCommand | None = None
    if backend == LOCAL_PTY_RUNTIME:
        cmd = reports.command(variant, registry, resolution)
        outcome = _supervised_bring_up(agent, ws, state, event, cmd, host=host, reports=reports)
        if outcome is not None:
            return outcome
        # The profile this tick read the backend off would not render a command, so the launch in
        # hand is the bare fallback invocation and a pane holds it after all.
        pending = cmd
    elif resolution is not None:
        pending = reports.command(variant, registry, resolution)
    active_report = _fresh_steward_report_in_progress(agent, time.time(), ws, state, host=host)
    if active_report:
        if pending is not None:
            # A launch diverted onto a pane profile builds its command, and the steward's
            # command carries a report card. This tick dispatches nothing, so that card is
            # closed through the same place every other outcome of this tick goes through.
            reports.undispatched(
                pending,
                "an earlier steward report of this role is still fresh, so this tick dispatched nothing.",
            )
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
            print(
                f"dispatch[{agent}]: no terminal visible yet but one was created "
                f"{time.time() - last_created:.1f}s ago — skipping to avoid a duplicate"
            )
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
                print(
                    f"dispatch[{agent}]: a ghost tab would not close (or tab list "
                    "unavailable) — not creating a fresh session this tick, next tick re-reaps"
                )
                return 0
            raw = _raw_terminal_count(ws, host=host)
            if raw is None:
                # The raw list itself failed (round 4, review B1): "unknown", not "zero". We
                # can't rule out a stray we'd be piling a fresh session on top of, so don't
                # create this tick -- the next one retries once Orca answers again.
                state.log_run(event, action="stray-check-failed")
                print(
                    f"dispatch[{agent}]: terminal list unavailable — skipping create to "
                    "avoid piling a fresh session on a possible stray"
                )
                return 0
            if raw > 0:
                # `_agent_terminals` recognized nothing, but Orca still lists a live terminal in
                # this workspace -- a stray it can't match by title/handle (an orphan from a
                # past incident, review B3). An ephemeral workspace's whole point is converging
                # to at most one terminal, so sweep it before creating rather than piling a
                # fresh session on top of an orphan that would otherwise run forever.
                if not _stop_and_confirm_workspace_empty(ws, host=host):
                    state.log_run(event, action="stray-sweep-failed")
                    print(
                        f"dispatch[{agent}]: could not confirm the workspace is clear of "
                        "stray terminals before creating — leaving it for the next tick"
                    )
                    return 0
                _, ok = _reap_ghosts(ws)
                if not ok:
                    # Stopped the stray's pty but a ghost tab wouldn't close: creating a fresh
                    # session now would leave the workspace above zero tabs, so bail and let the
                    # next tick re-reap before it creates (review B1, round 6).
                    state.log_run(event, action="stray-sweep-tab-failed")
                    print(
                        f"dispatch[{agent}]: swept stray terminal but a ghost tab would not "
                        "close; not creating this tick, next tick re-reaps"
                    )
                    return 0
        spawned = _spawn_fresh_terminal(
            agent,
            variant,
            ws,
            state,
            event,
            host=host,
            cmd=pending,
            snapshot=registry,
            resolution=resolution,
            reports=reports,
        )
        if isinstance(spawned, int):
            return spawned
        cmd = spawned
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
            print(
                f"dispatch[{agent}]: watchdog stop could not confirm the stuck terminal "
                "is gone — leaving it for the next tick"
            )
            return 0
        _, ok = _reap_ghosts(ws)
        if not ok:
            # Stopped the stuck pty but its ghost tab wouldn't close: don't spawn a replacement
            # next to a lingering tab, bail and let the next tick re-reap first (review B1).
            state.log_run(event, action="watchdog-restart-tab-failed")
            print(
                f"dispatch[{agent}]: watchdog stopped the stuck terminal but a ghost tab "
                "would not close; not restarting this tick, next tick re-reaps"
            )
            return 0
        spawned = _spawn_fresh_terminal(
            agent,
            variant,
            ws,
            state,
            event,
            host=host,
            cmd=pending,
            snapshot=registry,
            resolution=resolution,
            reports=reports,
        )
        if isinstance(spawned, int):
            return spawned
        cmd = spawned
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
            print(
                f"dispatch[{agent}]: ephemeral teardown could not confirm the finished "
                "terminal stopped — leaving it for the next tick"
            )
            return 0
        reaped, ok = _reap_ghosts(ws)
        if not ok:
            # Stopped the finished pty but its ghost tab wouldn't close: don't start a fresh
            # session next to a lingering tab, bail and let the next tick re-reap first. The
            # finished head's own finalizer trailer is the usual teardown path anyway; this
            # idle-restart branch is a backstop (review B1, round 6).
            state.log_run(event, action="ephemeral-restart-tab-failed")
            print(
                f"dispatch[{agent}]: ephemeral teardown stopped the finished terminal but a "
                "ghost tab would not close; not restarting this tick, next tick re-reaps"
            )
            return 0
        spawned = _spawn_fresh_terminal(
            agent,
            variant,
            ws,
            state,
            event,
            host=host,
            cmd=pending,
            snapshot=registry,
            resolution=resolution,
            reports=reports,
        )
        if isinstance(spawned, int):
            return spawned
        cmd = spawned
        state.log_run(event, action="ephemeral-restart")
        tail = f"; reaped {reaped} ghost(s)" if reaped else ""
        print(
            f"dispatch[{agent}]: ephemeral — torn down finished terminal, fresh session -> {cmd.skill}{tail}"
        )
        return 0

    # idle: a warm terminal keeps whatever profile it was spawned with, so a resource that's
    # gone red since spawn would otherwise get the skill anyway (only a fresh spawn
    # re-resolves). Stop it and start fresh on the resolved fallback instead — same shape as
    # the watchdog restart above — rather than leaving the red terminal running alongside a
    # new one, which would pile up one extra terminal per red tick (triggered-agents-274,
    # triggered-agents-275).
    if _reuse_head_is_red(agent, state, registry):
        if not _stop_and_confirm(ws, state, host=host):
            state.log_run(event, action="red-fallback-stop-failed")
            print(
                f"dispatch[{agent}]: red-fallback stop could not confirm the idle terminal "
                "stopped — leaving it for the next tick"
            )
            return 0
        spawned = _spawn_fresh_terminal(
            agent,
            variant,
            ws,
            state,
            event,
            host=host,
            cmd=pending,
            snapshot=registry,
            resolution=resolution,
            reports=reports,
        )
        if isinstance(spawned, int):
            return spawned
        cmd = spawned
        state.log_run(event, action="reused-red-fallback")
        print(
            f"dispatch[{agent}]: idle terminal's head is red — stopped, fresh fallback terminal -> {cmd.skill}"
        )
        return 0

    # idle: a terminal can remain live after its agent exits, leaving bash in the same pane.
    # `tui-idle` reports that shell as idle too, so inspect the rendered panel before any
    # slash command is sent. A dead REPL takes the normal stop/reap/fresh-create route.
    # Its telemetry action is `warm-repl-restart`, not `reused`.
    if not _agent_repl_visible(survivor.handle, host=host):
        if not _stop_and_confirm(ws, state, host=host):
            state.log_run(event, action="warm-repl-stop-failed")
            print(
                f"dispatch[{agent}]: idle terminal has no live agent REPL, but its stop "
                "could not be confirmed — leaving it for the next tick"
            )
            return 0
        _, ok = _reap_ghosts(ws)
        if not ok:
            state.log_run(event, action="warm-repl-restart-tab-failed")
            print(
                f"dispatch[{agent}]: idle terminal had no live agent REPL; stopped it but "
                "a ghost tab would not close, not restarting this tick"
            )
            return 0
        spawned = _spawn_fresh_terminal(
            agent,
            variant,
            ws,
            state,
            event,
            host=host,
            cmd=pending,
            snapshot=registry,
            resolution=resolution,
            reports=reports,
        )
        if isinstance(spawned, int):
            return spawned
        cmd = spawned
        state.log_run(event, action="warm-repl-restart")
        print(f"dispatch[{agent}]: idle terminal had no live agent REPL: fresh terminal -> {cmd.skill}")
        return 0

    # idle: warm reuse, killing nothing -> no ghost. Close only legacy duplicates (one-time).
    # The resolution comes first, before a single verb reaches this warm pane: the pre-scan
    # that let the tick get here read the registry earlier than the resolution does, so the
    # command this reuse would deliver can be one a supervisor holds. It is built here and
    # handed down rather than resolved inside the delivery, which keeps the tick to the one
    # resolution and the one report card it always had.
    pending = reports.command(variant, registry, resolution) if pending is None else pending
    supervised = _supervised_bring_up(agent, ws, state, event, pending, host=host, reports=reports)
    if supervised is not None:
        return supervised
    state.save_terminal_handle(survivor.handle)
    extras = [pane for pane in terms if pane.handle != survivor.handle]
    for pane in extras:
        _unchecked(lambda handle=pane.handle: host.close_pane(handle))
    _unchecked(lambda: host.send(survivor.handle, "/clear", enter=True))
    time.sleep(1.0)  # let /clear settle before the skill lands
    try:
        cmd = _send_reuse_dispatch(
            agent,
            variant,
            survivor.handle,
            ws,
            state,
            event,
            host=host,
            cmd=pending,
            snapshot=registry,
            resolution=resolution,
            reports=reports,
        )
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
