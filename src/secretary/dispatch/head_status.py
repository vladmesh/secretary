"""What the dispatcher's heads in one live workspace are, told apart from what its window shows.

An operator who opens a card's workspace and sees no worker pane reads it as "the worker never
started" and intervenes by hand -- drops the claim, kills the workspace, restarts the card -- and
destroys live work. The measurement behind ``issue:84c0ae4f796f994a7c1d`` (2026-08-24, card
secretary-1450) is the counter-example: pty 106 was listed by Orca with ``connected: true`` and
``paneRuntimeId: -1``, no runtime pane drew it, and the head behind it was working -- live TUI, a
growing rollout session, delivery accepted, vitality ``healthy_quiet``.

So this module answers two questions per head and keeps them apart:

    is the head alive?          from the vitality snapshot, and only from it
    is its runtime pane shown?  from the pane inventory, and never as evidence about the head

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
from secretary.dispatcher_review import (
    command_terminal_status,
    orca_worktree_panes,
    pane_matcher,
    worktree_panes,
)
from secretary.dispatcher_state import DispatcherRecord
from secretary.dispatcher_tui import provider_progress_for_persisted_run
from secretary.dispatcher_types import HostError
from secretary.dispatcher_watchdog import head_run_process_status, pid_file_path

# What this command may say about a head. Three words, deliberately: the two facts a snapshot can
# prove, and the honest third that keeps an unanswerable channel from being rounded to either.
HEAD_ALIVE = "alive"
HEAD_ABSENT = "absent"
HEAD_UNPROVEN = "unproven"

# What it may say about that head's runtime pane. "no-runtime-pane" is the case the card exists
# for and is a distinct word from "no-pane": the first is a pty Orca lists and does not draw, the
# second is a pty no inventory answers for. Neither is a word about the head.
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

    _inventory: dict[str, list[Any]] = field(default_factory=dict)

    def _worktree_terminals_or_raise(self, workspace: str) -> list[Any]:
        if workspace not in self._inventory:
            self._inventory[workspace] = orca_worktree_panes(self._run_json, workspace)
        return list(self._inventory[workspace])

    def provider_progress(
        self, _task: dict[str, Any], record: DispatcherRecord, kind: str
    ) -> dict[str, str]:
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
            "status": "degraded", "step": "head-status", "reason": "a workspace path is required",
            "workspace": "", "heads": [], "invariant": PANE_ADVISORY_INVARIANT,
        }
    mode = str(getattr(runtime.host, "mode", "real") or "real")
    if mode == "noop":
        # A noop host answers "live" to every question by construction. Reporting that as a head
        # would be exactly the fabricated observation the vitality vocabulary refuses to make.
        return {
            "status": "degraded", "step": "head-status", "workspace": target, "heads": [],
            "reason": "this dispatcher host is in noop mode and observes no live workspace",
            "invariant": PANE_ADVISORY_INVARIANT,
        }
    host = HeadStatusHost()
    payload = runtime.production_state.load()
    records = runtime.production_state.records(payload)
    panes, pane_channel = _pane_inventory(host, target)
    heads = [
        _head_row(
            host, ref, record,
            kind=kind, role=role, panes=panes, pane_channel=pane_channel, observed_at=observed_at,
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
        "heads": heads,
        "invariant": PANE_ADVISORY_INVARIANT,
        "summary": (
            [head["summary"] for head in heads]
            if heads
            else ["the dispatcher holds no head in this workspace"]
        ),
    }


def _pane_inventory(host: Any, workspace: str) -> tuple[list[Any], dict[str, str]]:
    """One inventory read for the whole answer, with its refusal kept as a channel fact."""
    try:
        return worktree_panes(host, workspace), {"state": "available", "reason": ""}
    except HostError as exc:
        return [], {"state": SourceAvailability.UNAVAILABLE.value, "reason": str(exc)[:240]}


def _head_row(
    host: Any,
    ref: str,
    record: DispatcherRecord,
    *,
    kind: str,
    role: str,
    panes: list[Any],
    pane_channel: dict[str, str],
    observed_at: float,
) -> dict[str, Any]:
    """One head: what proved it, what could not answer, and separately what the window shows."""
    run_payload = record.review_head_run if kind == "review" else record.worker_head_run
    run_id = str((run_payload or {}).get("run_id") or "")
    pane_state, pane_detail = _pane_axis(record, kind=kind, ref=ref, panes=panes, channel=pane_channel)
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
        episode=(
            {
                "run_id": bound_episode.run_id,
                "verdict": bound_episode.verdict.value,
                "basis": list(bound_episode.basis),
                "updated_at": bound_episode.updated_at,
            }
            if bound_episode is not None
            else None
        ),
        reason=refusal,
    )
    if episode is not None and bound_episode is None:
        # Someone else's conclusion about someone else's run. Naming the refusal is what keeps a
        # stale episode from being read as this head's silence.
        row["episode_note"] = "the persisted vitality episode names another run and was not used"
    row["summary"] = _summary(row)
    return row


_REPORTED_SOURCES = (
    SnapshotSource.PID_HEARTBEAT,
    SnapshotSource.PROVIDER_CURSOR,
    SnapshotSource.PANE_ADVISORY,
)


def _terminal_status(
    host: Any, ref: str, record: DispatcherRecord, kind: str
) -> tuple[dict[str, Any], str]:
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
            pid_file_path(kind, ref), run=run, role=kind, task=f"card:{ref}", leaf=leaf,
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
) -> tuple[str, dict[str, Any] | None]:
    """Whether this head's pty is drawn in the workspace, as the inventory itself reports it."""
    if channel.get("state") != "available":
        return PANE_UNAVAILABLE, None
    matches = pane_matcher(record, kind=kind, task_ref=ref)
    pane = next((candidate for candidate in panes if matches(candidate)), None)
    if pane is None:
        return PANE_NO_PANE, None
    detail = {
        "handle": pane.handle,
        "leaf": pane.leaf,
        "title": pane.title,
        "connected": bool(pane.connected),
        "runtime_pane_id": pane.runtime_pane_id,
    }
    if pane.runtime_pane_id is None:
        return PANE_UNKNOWN, detail
    return (PANE_VISIBLE if pane.runtime_pane_id >= 0 else PANE_NO_RUNTIME_PANE), detail


# What the pane half of a summary says, and what every answer but "visible" ends in. The closing
# clause is the whole point of the sentence: it is read by someone standing in front of a
# workspace that looks empty, and it must leave them unable to conclude anything about the head.
_NOT_ABOUT_THE_HEAD = "that is a fact about the window, not about the head"


def _pane_sentence(row: dict[str, Any]) -> str:
    """The pane half of the answer, always ending in what it does not mean."""
    pane = row.get("pane") or {}
    runtime_pane_id = pane.get("runtime_pane_id")
    state = row["runtime_pane"]
    if state == PANE_VISIBLE:
        return f"Its runtime pane is visible (paneRuntimeId {runtime_pane_id})."
    if state == PANE_NO_RUNTIME_PANE:
        return (
            f"Its runtime pane is NOT visible: the session manager lists the pty "
            f"(connected={str(bool(pane.get('connected'))).lower()}) with paneRuntimeId "
            f"{runtime_pane_id}, so nothing draws it in the workspace; {_NOT_ABOUT_THE_HEAD}."
        )
    if state == PANE_NO_PANE:
        return (
            f"No pane in the workspace inventory answers to this head; {_NOT_ABOUT_THE_HEAD}."
        )
    if state == PANE_UNAVAILABLE:
        return (
            "The pane inventory could not be read, so nothing is known about its pane; "
            "that is a fact about that channel, not about the head."
        )
    return (
        "The session manager named no runtime pane for this pty, so its visibility is unknown; "
        f"{_NOT_ABOUT_THE_HEAD}."
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
        verdict = (
            f"{who} is ABSENT: {row['proved_by']} says the process behind its launch identity "
            "is gone."
        )
    else:
        dark = ", ".join(row["unavailable_sources"]) or "none of the sources answered"
        verdict = (
            f"{who} is UNPROVEN: no source proved it either way (unavailable: {dark}). "
            "This is a statement about the observation, not about the head."
        )
    return f"{verdict} {_pane_sentence(row)}"


def _normalised(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path or ""))) if path else ""
