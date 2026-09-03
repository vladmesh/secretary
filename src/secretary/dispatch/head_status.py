"""What the dispatcher's heads in one live workspace are, told apart from what its window shows.

An operator who opens a card's workspace and sees no worker pane reads it as "the worker never
started" and intervenes by hand -- drops the claim, kills the workspace, restarts the card -- and
destroys live work. The measurement behind ``issue:84c0ae4f796f994a7c1d`` (2026-08-24, card
secretary-1450) is the counter-example: pty 106 was listed by Orca with ``connected: true``, no
runtime pane drew it, and the head behind it was working -- live TUI, a growing rollout session,
delivery accepted, vitality ``healthy_quiet``.

So this module answers two questions per head and keeps them apart:

    is the head alive?          from the vitality snapshot, and only from it
    is its runtime pane shown?  from the renderer's own tree, never as evidence about the head

The second question is asked of the thing that actually answers it. `orca terminal list` describes
ptys and says nothing about what is drawn; the visual-layout tree it returns beside them, with
`--include-visual-layouts`, is the renderer's own inventory of drawn panes. So visibility here is
membership: a pty the tree names is drawn, and a pty the inventory lists while no leaf of that tree
names it is the measured case -- listed, connected, drawn by nothing.

The whole point is the second one can never answer the first. ``head_vitality``'s invariant --
pane and terminal readings are advisory, fill the ``Turn`` axis alone and are never by themselves
evidence of death -- is what this command exists to make visible to a human, so it is repeated in
the output rather than merely obeyed in the code.

Read-only, in the strong sense: it starts nothing, stops nothing, repairs nothing and writes
neither the dispatcher's state nor the head's. Its host is a transport that can only read, so a
provider cursor is read from the run already persisted rather than being rebound, and a head whose
channel cannot answer is reported unproven rather than probed harder.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from secretary import _proc
from secretary.dispatch.head_vitality import (
    ProcessState,
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    VitalitySnapshot,
    snapshots_from_status,
)
from secretary.dispatch.head_vitality_episode import recovery_outlook
from secretary.dispatcher_review import (
    command_terminal_status,
    orca_workspace_inventory,
    pane_matcher,
)
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_tui import provider_progress_for_persisted_run
from secretary.dispatcher_types import HostError
from secretary.dispatcher_watchdog import head_run_process_status, pid_file_path
from triggered_agents.runtime.pane_host import RuntimeLayout, WorkspaceInventory

# What this command may say about a head. Three words, deliberately: the two facts a snapshot can
# prove, and the honest third that keeps an unanswerable channel from being rounded to either.
HEAD_ALIVE = "alive"
HEAD_ABSENT = "absent"
HEAD_UNPROVEN = "unproven"

# What it may say about that head's runtime pane. Four negative words where one would do, because
# each names a different fact: "no-runtime-pane" is a pty the inventory lists and the renderer
# draws nowhere (the case the card exists for), "no-pane" is a pty no inventory answers for at all,
# "unknown" is a renderer channel that could not decide -- unsupported by this build, silent about
# this workspace, or naming no identity this pty can be compared by -- and "unavailable" is a pane
# inventory that refused. None of the four is a word about the head.
PANE_VISIBLE = "visible"
PANE_NO_RUNTIME_PANE = "no-runtime-pane"
PANE_NO_PANE = "no-pane"
PANE_UNKNOWN = "unknown"
PANE_UNAVAILABLE = "unavailable"

# Printed on every answer, next to every head. An operator reading a row must not have to know the
# module invariant to read the row correctly.
PANE_ADVISORY_INVARIANT = (
    "pane readings are advisory: a pane with no runtime pane, a disconnected pane, a pane no "
    "inventory names and an unreadable pane channel are all facts about the window, and none of "
    "them is evidence that a head is absent"
)

# A source the observation carried no reading from at all. Distinct from `unavailable`, which is a
# channel that was asked and could not answer, because reporting the two as one word would make an
# unsampled source look like a broken one.
NOT_OBSERVED = "not_observed"

_ROLES = (("worker", "worker"), ("review", "reviewer"))


@dataclass
class ReadOnlyOrcaTransport:
    """The ``orca terminal`` JSON transport, for an observer that may only read.

    The same shape the dispatcher host exposes to `command_terminal_status`, minus everything that
    could change the session manager's state: this object owns no lifecycle call, so a caller
    holding one cannot open, close, type into or stop a pane even by mistake.
    """

    mode: str = "real"

    def _run_json(self, args: list[str]) -> dict[str, Any]:
        try:
            completed = _proc.run(args, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(f"terminal inventory unavailable: {exc}") from None
        if completed.returncode:
            raise HostError("terminal inventory failed")
        try:
            payload = json.loads(completed.stdout or "{}")
        except ValueError:
            raise HostError("terminal inventory returned invalid JSON") from None
        return payload.get("result", payload) if isinstance(payload, dict) else {}


@dataclass
class HeadStatusHost(ReadOnlyOrcaTransport):
    """Everything `command_terminal_status` asks a host for, answered without a single write.

    Two differences from the dispatcher's own host, and both are the point. The worktree inventory
    is read once and reused, so every head in one workspace is reported against the same pane
    inventory rather than against three successive ones that may disagree. And the provider cursor
    comes from the run already persisted on the record: the dispatcher's own reader may bind a
    Claude source and commit that binding back onto the record, which is a write, and an observer
    reports "this channel did not answer" instead of performing one.
    """

    _inventory: dict[str, WorkspaceInventory] = field(default_factory=dict)

    def workspace_inventory(self, workspace: str) -> WorkspaceInventory:
        """One reading of the workspace -- its ptys and its renderer tree -- for every head."""
        if workspace not in self._inventory:
            self._inventory[workspace] = orca_workspace_inventory(self._run_json, workspace)
        return self._inventory[workspace]

    def _worktree_terminals_or_raise(self, workspace: str) -> list[Any]:
        # The seam `command_terminal_status` finds by name, answered from the same single reading:
        # the head axis and the pane axis of one row cannot then disagree about which ptys existed.
        return list(self.workspace_inventory(workspace).panes)

    def provider_progress(self, _task: dict[str, Any], record: DispatcherRecord, kind: str) -> dict[str, str]:
        run = record.review_head_run if kind == "review" else record.worker_head_run
        return provider_progress_for_persisted_run(run)


def head_status(runtime: Any, *, workspace: str, now: float | None = None) -> dict[str, Any]:
    """Answer, for every head the dispatcher holds in ``workspace``, alive and pane-shown apart.

    One row per head identity the dispatcher actually carries for that workspace, so a workspace
    the dispatcher holds nothing in answers with no rows rather than with a guess. Nothing here
    consults a wait ceiling, a threshold or a recovery ladder: the row states what the sources
    said, which of them could not answer, and which one proved what.
    """
    observed_at = time.time() if now is None else float(now)
    target = _normalised(workspace)
    if not target:
        return {
            "status": "degraded",
            "step": "head-status",
            "reason": "a workspace path is required",
            "workspace": "",
            "heads": [],
            "invariant": PANE_ADVISORY_INVARIANT,
        }
    mode = str(getattr(runtime.host, "mode", "real") or "real")
    if mode == "noop":
        # A noop host answers "live" to every question by construction. Reporting that as a head
        # would be exactly the fabricated observation the vitality vocabulary refuses to make.
        return {
            "status": "degraded",
            "step": "head-status",
            "workspace": target,
            "heads": [],
            "reason": "this dispatcher host is in noop mode and observes no live workspace",
            "invariant": PANE_ADVISORY_INVARIANT,
        }
    host = HeadStatusHost()
    payload = runtime.production_state.load()
    records = runtime.production_state.records(payload)
    panes, pane_channel, layout = _pane_inventory(host, target)
    heads = [
        _head_row(
            host,
            ref,
            record,
            kind=kind,
            role=role,
            panes=panes,
            pane_channel=pane_channel,
            layout=layout,
            observed_at=observed_at,
        )
        for ref, record in sorted(records.items())
        if _normalised(record.workspace) == target
        for kind, role in _ROLES
        if record.owns_head(kind)
    ]
    return {
        "status": "ok",
        "step": "head-status",
        "workspace": target,
        "pane_channel": pane_channel,
        "runtime_pane_channel": _layout_channel(layout),
        "heads": heads,
        "invariant": PANE_ADVISORY_INVARIANT,
        "summary": (
            [head["summary"] for head in heads]
            if heads
            else ["the dispatcher holds no head in this workspace"]
        ),
    }


def _pane_inventory(host: Any, workspace: str) -> tuple[list[Any], dict[str, str], RuntimeLayout | None]:
    """One inventory read for the whole answer, with its refusal kept as a channel fact.

    Two channels come back, not one, and they fail apart: the session manager can list a
    workspace's ptys perfectly while saying nothing about what its renderer draws. Folding the
    second refusal into the first would report a readable workspace as unreadable; folding it the
    other way would report an unread tree as an undrawn pane, which is the same lie this command
    exists to stop, told from the other side.
    """
    try:
        inventory = host.workspace_inventory(workspace)
    except HostError as exc:
        return [], {"state": SourceAvailability.UNAVAILABLE.value, "reason": str(exc)[:240]}, None
    return list(inventory.panes), {"state": "available", "reason": ""}, inventory.layout


def _layout_channel(layout: RuntimeLayout | None) -> dict[str, Any]:
    """The renderer channel, in the vitality vocabulary: available, or unavailable and why."""
    if layout is None or not layout.supported or not layout.known_workspace:
        return {
            "state": SourceAvailability.UNAVAILABLE.value,
            "reason": (
                (layout.reason if layout is not None else "")
                or "the pane inventory could not be read, so neither could the renderer tree"
            )[:240],
            "supported": bool(layout is not None and layout.supported),
        }
    return {
        "state": SourceAvailability.AVAILABLE.value,
        "reason": "",
        "supported": True,
        "drawn_panes": layout.terminal_nodes,
    }


def _head_row(
    host: Any,
    ref: str,
    record: DispatcherRecord,
    *,
    kind: str,
    role: str,
    panes: list[Any],
    pane_channel: dict[str, str],
    layout: RuntimeLayout | None,
    observed_at: float,
) -> dict[str, Any]:
    """One head: what proved it, what could not answer, and separately what the window shows."""
    run_payload = record.review_head_run if kind == "review" else record.worker_head_run
    run_id = str((run_payload or {}).get("run_id") or "")
    pane_state, pane_detail = _pane_axis(
        record,
        kind=kind,
        ref=ref,
        panes=panes,
        channel=pane_channel,
        layout=layout,
    )
    row: dict[str, Any] = {
        "ref": ref,
        "role": role,
        "run_id": run_id or None,
        "card_state": record.state,
        "runtime_pane": pane_state,
        "pane": pane_detail,
        "invariant": PANE_ADVISORY_INVARIANT,
    }
    if not run_id:
        # Criterion of the whole vocabulary: a snapshot is bound to a run. With no durable HeadRun
        # there is nothing to bind one to, and borrowing another run's evidence is the exact lie
        # the binding exists to prevent -- so the row says so and proves nothing.
        row.update(
            head=HEAD_UNPROVEN,
            process=ProcessState.UNKNOWN.value,
            proved_by=None,
            evidence=[_not_observed(source) for source in _REPORTED_SOURCES],
            unavailable_sources=[],
            episode=None,
            reason=(
                "the dispatcher holds a head identity for this role but no durable HeadRun, so no "
                "vitality snapshot may be bound to it"
            ),
        )
        row["summary"] = _summary(row)
        return row
    status, refusal = _terminal_status(host, ref, record, kind)
    episode = record.review_vitality_episode if kind == "review" else record.worker_vitality_episode
    bound_episode = episode if episode is not None and episode.run_id == run_id else None
    snapshots = snapshots_from_status(
        status,
        run_id=run_id,
        previous_cursor=(
            (bound_episode.evidence_cursors or {}).get(SnapshotSource.PROVIDER_CURSOR.value, "")
            if bound_episode is not None
            else ""
        ),
        observed_at=observed_at,
    )
    by_source = {snapshot.source: snapshot for snapshot in snapshots}
    head, process, proved_by = _verdict(by_source)
    row.update(
        head=head,
        process=process.value,
        proved_by=proved_by.value if proved_by is not None else None,
        evidence=[_evidence(source, by_source.get(source)) for source in _REPORTED_SOURCES],
        unavailable_sources=sorted(
            source.value
            for source, snapshot in by_source.items()
            if snapshot.availability is SourceAvailability.UNAVAILABLE
        ),
        episode=_episode_row(bound_episode, record, kind=kind, observed_at=observed_at),
        reason=refusal,
    )
    if episode is not None and bound_episode is None:
        # Someone else's conclusion about someone else's run. Naming the refusal is what keeps a
        # stale episode from being read as this head's silence.
        row["episode_note"] = "the persisted vitality episode names another run and was not used"
    row["summary"] = _summary(row)
    return row


def _episode_row(
    episode: Any,
    record: DispatcherRecord,
    *,
    kind: str,
    observed_at: float,
) -> dict[str, Any] | None:
    """The persisted conclusion, plus what an operator needs when a head goes quiet.

    A head frozen behind a dark progress source used to be readable only as
    ``healthy_quiet`` plus a basis token, which is precisely the state
    ``issue:7bff833fef6d9d9b404d`` sat in for 65 minutes. So the row answers the four questions
    that state raises: which progress source is missing or dark and since when, how long the head
    has been quiet, what the last meaningful progress was (the episode's own advancement, and the
    card's pane-output stamp beside it), and when the next recovery rung falls due.

    Derived, never re-observed: every number comes from the persisted episode and the record this
    command already read, through the reducer's own arithmetic (``recovery_outlook``).
    """
    if episode is None:
        return None
    outlook = recovery_outlook(episode, observed_at)
    return {
        "run_id": episode.run_id,
        "verdict": episode.verdict.value,
        "basis": list(episode.basis),
        "updated_at": episode.updated_at,
        "reason": episode.reason,
        "quiet_seconds": outlook["quiet_seconds"],
        # Both halves of "which source is missing": one that answered and stopped is dark with a
        # stamp, one this observation never carried is simply absent from the map.
        "dark_progress_sources": outlook["dark_sources"],
        "missing_progress_sources": [
            source.value
            for source in _REPORTED_SOURCES
            if source is SnapshotSource.PROVIDER_CURSOR
            and source.value not in episode.evidence_cursors
            and source.value not in episode.unavailable_since
        ],
        "last_progress": {
            "at": episode.last_progress_at,
            "source": episode.last_progress_source or "",
            # The workspace half: the card's own pane-output stamp, which is not vitality
            # evidence and is reported beside the episode rather than folded into it.
            "pane_output_at": float(getattr(record, f"{kind}_progress_at", 0.0) or 0.0),
            "waiting_since": float(getattr(record, f"{kind}_waiting_since", 0.0) or 0.0),
        },
        "next_recovery_deadline": outlook["next_deadline"],
        "deadline_note": outlook["note"],
        "answer_owed_since": (float(record.worker_answer_owed_since or 0.0) if kind == "worker" else 0.0),
        "turn_ended_at": episode.turn_ended_at,
    }


_REPORTED_SOURCES = (
    SnapshotSource.PID_HEARTBEAT,
    SnapshotSource.PROVIDER_CURSOR,
    SnapshotSource.PANE_ADVISORY,
)


def _terminal_status(host: Any, ref: str, record: DispatcherRecord, kind: str) -> tuple[dict[str, Any], str]:
    """The observation the wait tick makes, falling back to the pid heartbeat alone.

    `command_terminal_status` reaches the heartbeat through the pane inventory, so an inventory
    that refuses takes the whole observation with it -- and an operator looking at a workspace
    whose session manager is unreachable is exactly the person who must not be told the head is
    gone. So the refusal is recorded as a fact about that channel and the same pid probe the wait
    tick would have made is made directly: one source instead of three, and the row says so.
    """
    try:
        return command_terminal_status(host, {"ref": ref}, record, kind=kind), ""
    except HostError as exc:
        run = record.review_head_run if kind == "review" else record.worker_head_run
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        pid_status = head_run_process_status(
            pid_file_path(kind, ref),
            run=run,
            role=kind,
            task=f"card:{ref}",
            leaf=leaf,
        )
        return {"pid_status": dict(pid_status)}, (
            f"the pane channel could not be read ({str(exc)[:200]}), so this head was observed "
            "through its pid heartbeat alone"
        )


def _verdict(
    by_source: dict[SnapshotSource, VitalitySnapshot],
) -> tuple[str, ProcessState, SnapshotSource | None]:
    """Alive, absent or unproven -- from the snapshots alone, and never from the pane.

    The pid heartbeat is the only source that may say a head is gone, because it is the only one
    that observes the process. A bound provider cursor that moved proves the opposite direction
    only: work advanced, so something is running it. Everything else leaves the answer unproven,
    which is a statement about this observation and not about the head.
    """
    pid = by_source.get(SnapshotSource.PID_HEARTBEAT)
    if pid is not None and pid.availability is SourceAvailability.AVAILABLE:
        if pid.process in (ProcessState.RUNNING, ProcessState.SUSPENDED):
            return HEAD_ALIVE, pid.process, SnapshotSource.PID_HEARTBEAT
        if pid.process is ProcessState.DEAD:
            return HEAD_ABSENT, ProcessState.DEAD, SnapshotSource.PID_HEARTBEAT
    provider = by_source.get(SnapshotSource.PROVIDER_CURSOR)
    if (
        provider is not None
        and provider.availability is SourceAvailability.AVAILABLE
        and provider.progress is ProgressState.ADVANCING
    ):
        return HEAD_ALIVE, ProcessState.UNKNOWN, SnapshotSource.PROVIDER_CURSOR
    return HEAD_UNPROVEN, ProcessState.UNKNOWN, None


def _evidence(source: SnapshotSource, snapshot: VitalitySnapshot | None) -> dict[str, Any]:
    """What one channel said, in the snapshot's own words."""
    if snapshot is None:
        return _not_observed(source)
    return {
        "source": source.value,
        "availability": snapshot.availability.value,
        "answered": snapshot.availability is SourceAvailability.AVAILABLE,
        "advisory": snapshot.advisory,
        "process": snapshot.process.value,
        "turn": snapshot.turn.value,
        "progress": snapshot.progress.value,
        "reason": snapshot.reason,
    }


def _not_observed(source: SnapshotSource) -> dict[str, Any]:
    return {
        "source": source.value,
        "availability": NOT_OBSERVED,
        "answered": False,
        "advisory": source is SnapshotSource.PANE_ADVISORY,
        "process": ProcessState.UNKNOWN.value,
        "turn": "unknown",
        "progress": ProgressState.UNKNOWN.value,
        "reason": "this observation carried no reading from this channel",
    }


def _pane_axis(
    record: DispatcherRecord,
    *,
    kind: str,
    ref: str,
    panes: list[Any],
    channel: dict[str, str],
    layout: RuntimeLayout | None,
) -> tuple[str, dict[str, Any] | None]:
    """Whether this head's pty is drawn in the workspace, as the renderer itself reports it."""
    if channel.get("state") != "available":
        return PANE_UNAVAILABLE, None
    matches = pane_matcher(record, kind=kind, task_ref=ref)
    pane = next((candidate for candidate in panes if matches(candidate)), None)
    if pane is None:
        return PANE_NO_PANE, None
    state, reason = _drawn(pane, layout)
    detail = {
        "handle": pane.handle,
        "leaf": pane.leaf,
        "title": pane.title,
        "connected": bool(pane.connected),
        # Supplementary only, and usually absent: the build measured on 2026-08-25 returns no
        # `paneRuntimeId` from this call. Reported where a host does name it, never relied on.
        "runtime_pane_id": pane.runtime_pane_id,
        "renderer_reason": reason,
    }
    return state, detail


def _drawn(pane: Any, layout: RuntimeLayout | None) -> tuple[str, str]:
    """Membership of this pty in the renderer tree, and the one honest word for each outcome.

    Identity is the whole difficulty. `terminal list` can hand back a different handle alias for
    the same pty (`dispatcher_state.py:132`), so `leafId` is the primary key and the handle is only
    a secondary one -- and an identity that cannot be compared at all is `unknown`, never a denial.
    A negative verdict is therefore licensed only when the key that decides it is usable: a tree
    that named leaves, or a tree that draws nothing at all.
    """
    if layout is None or not layout.supported:
        return PANE_UNKNOWN, (layout.reason if layout is not None else "") or (
            "the renderer tree was not read"
        )
    if not layout.known_workspace:
        return PANE_UNKNOWN, layout.reason
    if pane.leaf and pane.leaf in layout.leaves:
        return PANE_VISIBLE, ("the renderer tree of this workspace draws a pane with this pty's leaf")
    if pane.handle and pane.handle in layout.handles:
        return PANE_VISIBLE, ("the renderer tree of this workspace draws a pane with this pty's handle")
    empty_tree = layout.terminal_nodes == 0
    if pane.leaf and (layout.leaves or empty_tree):
        return PANE_NO_RUNTIME_PANE, (
            f"the inventory lists this pty and no pane of the workspace's renderer tree "
            f"({layout.terminal_nodes} drawn) names its leaf"
        )
    if not pane.leaf and pane.handle and (layout.handles or empty_tree):
        return PANE_NO_RUNTIME_PANE, (
            f"no leaf was ever persisted for this pty and no pane of the renderer tree "
            f"({layout.terminal_nodes} drawn) names its handle"
        )
    return PANE_UNKNOWN, (
        "the renderer tree named no identity this pty can be compared by, and a handle the "
        "session "
        "manager may have aliased is not evidence either way"
    )


# What the pane half of a summary says, and what every answer but "visible" ends in. The closing
# clause is the whole point of the sentence: it is read by someone standing in front of a
# workspace that looks empty, and it must leave them unable to conclude anything about the head.
_NOT_ABOUT_THE_HEAD = "that is a fact about the window, not about the head"


def _pane_sentence(row: dict[str, Any]) -> str:
    """The pane half of the answer, always ending in what it does not mean."""
    pane = row.get("pane") or {}
    reason = str(pane.get("renderer_reason") or "")
    state = row["runtime_pane"]
    if state == PANE_VISIBLE:
        return f"Its runtime pane is visible: {reason}."
    if state == PANE_NO_RUNTIME_PANE:
        return (
            f"Its runtime pane is NOT visible: {reason}, and the pty is "
            f"connected={str(bool(pane.get('connected'))).lower()}, so nothing draws it in the "
            f"workspace; {_NOT_ABOUT_THE_HEAD}."
        )
    if state == PANE_NO_PANE:
        return f"No pane in the workspace inventory answers to this head; {_NOT_ABOUT_THE_HEAD}."
    if state == PANE_UNAVAILABLE:
        return (
            "The pane inventory could not be read, so nothing is known about its pane; "
            "that is a fact about that channel, not about the head."
        )
    return (
        f"Whether its runtime pane is visible is unknown: {reason}; "
        "that is a fact about that channel, not about the head."
    )


def _summary(row: dict[str, Any]) -> str:
    """One sentence an operator can act on without interpreting anything.

    Deliberately says the head half first and the pane half second, and never lets the second
    qualify the first: the failure this card exists to stop is a human reading an invisible pane
    as a missing head.
    """
    head = row["head"]
    run = row["run_id"] or "no run id"
    who = f"{row['role']} head of {row['ref']} (run {run})"
    if head == HEAD_ALIVE:
        proof = (
            f"{row['proved_by']} says its process is {row['process']}"
            if row["proved_by"] == SnapshotSource.PID_HEARTBEAT.value
            else f"{row['proved_by']} says its work advanced"
        )
        verdict = f"{who} is ALIVE: {proof}."
    elif head == HEAD_ABSENT:
        verdict = f"{who} is ABSENT: {row['proved_by']} says the process behind its launch identity is gone."
    else:
        dark = ", ".join(row["unavailable_sources"]) or "none of the sources answered"
        verdict = (
            f"{who} is UNPROVEN: no source proved it either way (unavailable: {dark}). "
            "This is a statement about the observation, not about the head."
        )
    return f"{verdict} {_episode_sentence(row)}{_pane_sentence(row)}"


def _episode_sentence(row: dict[str, Any]) -> str:
    """The quiet half, said only when there is something an operator has to act on.

    A live head whose progress channel is dark reads as ``alive`` on the head axis and as an
    ordinary pane on the pane axis, and until secretary-1543 that is all the summary said -- the
    one line an operator reads named neither the missing channel nor the deadline. It does now,
    and only then: a head with no dark source and no pending rung adds no words.
    """
    episode = row.get("episode") or {}
    dark = episode.get("dark_progress_sources") or []
    deadline = episode.get("next_recovery_deadline") or {}
    if not dark and not deadline:
        return ""
    parts = []
    if dark:
        parts.append(
            "; ".join(f"{entry['source']} has been dark for {int(entry['dark_seconds'])}s" for entry in dark)
        )
    if deadline:
        parts.append(
            f"it becomes {deadline['verdict']} in {int(deadline['in_seconds'])}s unless something advances"
        )
    elif episode.get("deadline_note"):
        parts.append(str(episode["deadline_note"]))
    return f"Vitality {episode.get('verdict', 'unknown')}: " + ", and ".join(parts) + ". "


def _normalised(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path or ""))) if path else ""
