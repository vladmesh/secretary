"""Strict, launch-bound ingestion of Codex's structured session event journal.

This is intentionally separate from the inexpensive workspace activity lookup used by the
watchdog.  Activity can tolerate a stale or malformed session file; a provider-policy decision
cannot.  A source is accepted only after it is bound to the exact, already-attested ``HeadRun``
and its cursor is durably written.  Every later read re-proves that binding before consuming a
new line.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from triggered_agents.runtime.codex_preflight import (
    CodexFanoutPolicyError,
    CodexFanoutRecordingError,
    CodexProviderEventRecorder,
    EVENT_CHILD_THREAD_EDGE,
    EVENT_COLLABORATION_CALL,
    EVENT_UNKNOWN_THREAD_EDGE,
    EVENT_UNPARSEABLE_PROVIDER_EVENT,
    FANOUT_TERMINAL_UNKNOWN,
    enforce_provider_event,
)
from triggered_agents.runtime.head import HeadRun


SOURCE_VERSION = 1
SOURCE_KIND = "codex_session_event_jsonl"


class CodexProviderSourceError(CodexFanoutPolicyError):
    """The run cannot safely attribute a structured provider-event source to itself."""


@dataclass(frozen=True)
class SourceLine:
    """One physical provider event line, including the durable cursor that names it."""

    number: int
    raw: str
    digest: str
    event: Any


class CodexProviderEventIngress:
    """One durable Codex event source, its exact ``HeadRun`` and its fenced consequences.

    ``persist`` is the same lifecycle writer used for the role's HeadRun.  ``stop`` must identity
    fence before touching a pane or pid; this class deliberately has no backend capability of its
    own.  ``block`` is the card/sprint transition's typed-evidence path.
    """

    def __init__(
        self,
        run: HeadRun,
        persist: Callable[[HeadRun], None],
        *,
        stop: Callable[[HeadRun, str], None],
        block: Callable[[dict[str, Any]], None],
    ) -> None:
        self.run = run
        self.persist = persist
        self.stop = stop
        self.block = block

    @property
    def source(self) -> dict[str, Any]:
        value = self.run.fanout_policy.get("provider_source")
        return dict(value) if isinstance(value, dict) else {}

    def commit_run(self, run: HeadRun) -> None:
        """Persist the handle/leaf rebinding before source binding or prompt delivery."""
        if not self.run.same_run(run):
            raise CodexProviderSourceError(
                "Codex provider ingress was handed another HeadRun", run=self.run
            )
        self.persist(run)
        self.run = run

    def bind_before_delivery(self) -> None:
        """Bind and scan a Codex session before any prompt is sent.

        Binding is not a permission to skip what the provider wrote while the pane was coming
        up.  The durable parent cursor is the only point at which a source belongs to this run;
        once it is committed, ``poll`` is the one classifier for both the pre-existing tail and
        every later lifecycle poll.  A policy result raises after its fenced stop/block callbacks,
        so it cannot fall through to prompt delivery.
        """
        source = self.source
        if str(source.get("state") or "") == "bound":
            self.poll()
            return
        if str(source.get("state") or "") != "unbound":
            self._unknown("Codex provider source binding is missing or malformed")
            return
        root = Path(str(source.get("root") or ""))
        if not root.is_dir():
            self._unknown("Codex provider session source root is unavailable")
            return
        baseline = source.get("baseline")
        if not isinstance(baseline, list) or not all(isinstance(path, str) for path in baseline):
            self._unknown("Codex provider source baseline is malformed")
            return
        candidates: list[tuple[Path, dict[str, Any], list[SourceLine]]] = []
        try:
            paths = list(root.rglob("*.jsonl"))
            root_resolved = root.resolve(strict=True)
        except OSError:
            self._unknown("Codex provider session source cannot be enumerated")
            return
        baseline_paths = set(baseline)
        for path in paths:
            try:
                resolved_path = path.resolve(strict=True)
                if not resolved_path.is_relative_to(root_resolved):
                    continue
                resolved = str(resolved_path)
            except (OSError, ValueError):
                continue
            if resolved in baseline_paths:
                continue
            parsed = _read_source(path)
            if parsed is None:
                continue
            meta, lines = parsed
            if str(meta.get("cwd") or "") != str(Path(self.run.workspace).resolve(strict=False)):
                continue
            parent = _parent_thread(lines)
            session_id = str(meta.get("session_id") or "")
            if not session_id or not parent:
                continue
            candidates.append((path, {"session_id": session_id, "parent_thread_id": parent}, lines))
        if len(candidates) != 1:
            self._unknown(
                "Codex provider source is unbound: expected exactly one new session with a parent thread"
            )
            return
        path, identity, lines = candidates[0]
        parent_line = next(
            line for line in lines
            if isinstance(line.event, dict)
            and line.event.get("type") == "thread.started"
            and line.event.get("thread_id") == identity["parent_thread_id"]
        )
        bound = {
            "version": SOURCE_VERSION,
            "kind": SOURCE_KIND,
            "state": "bound",
            "root": str(root.resolve(strict=False)),
            "path": str(path.resolve(strict=False)),
            "session_id": identity["session_id"],
            "parent_thread_id": identity["parent_thread_id"],
            "cursor": {"line": parent_line.number, "digest": parent_line.digest},
            "bound_at": _now(),
        }
        self._replace_source(bound)
        # Do not merely advance the initial cursor.  Lines that appeared after the parent before
        # this callback are already provider evidence for this exact source and must take this
        # same durable recorder/identity-fenced stop path before the caller can type a prompt.
        self.poll()

    def poll(self) -> None:
        """Consume all new provider events, verifying binding and cursor before each action."""
        source = self.source
        if str(source.get("state") or "") != "bound":
            self._unknown("Codex provider source is not bound during lifecycle recovery")
            return
        parsed = self._verify_binding(source)
        if parsed is None:
            return
        _meta, lines = parsed
        cursor = source.get("cursor")
        if not isinstance(cursor, dict):
            self._unknown("Codex provider source cursor is missing")
            return
        cursor_line = cursor.get("line")
        cursor_digest = str(cursor.get("digest") or "")
        if not isinstance(cursor_line, int) or cursor_line < 1 or not cursor_digest:
            self._unknown("Codex provider source cursor is malformed")
            return
        prior = next((line for line in lines if line.number == cursor_line), None)
        if prior is None or prior.digest != cursor_digest:
            self._unknown("Codex provider source cursor no longer matches the bound event journal")
            return
        fresh = [line for line in lines if line.number > cursor_line]
        for line in fresh:
            events = list(
                _provider_events(
                    line.event,
                    str(source.get("parent_thread_id") or ""),
                    raw_event_digest=line.digest,
                )
            )
            if not events:
                self._advance_cursor(source, line)
                continue
            for raw_event in events:
                run = self._run_at_cursor(source, line)
                recorder = CodexProviderEventRecorder(
                    run, self._persist, expected_parent_thread_id=str(source.get("parent_thread_id") or ""),
                )
                try:
                    outcome = enforce_provider_event(
                        recorder,
                        raw_event,
                        source_sequence=line.number,
                        source_location=f"{source.get('path')}:{line.number}",
                        stop=self.stop,
                        block=self.block,
                    )
                except CodexFanoutRecordingError:
                    self.run = recorder.run
                    raise
                self.run = outcome.run
                # Any non-clean event already went through the fenced stop and block callbacks.
                if outcome.blocked:
                    raise CodexProviderSourceError(
                        "Codex provider fan-out policy blocked this HeadRun", run=outcome.run
                    )
            # A source line which resulted in events was persisted by the recorder at its cursor.

    def _verify_binding(self, source: Mapping[str, Any]) -> tuple[dict[str, Any], list[SourceLine]] | None:
        if (
            source.get("version") != SOURCE_VERSION
            or source.get("kind") != SOURCE_KIND
            or not str(source.get("path") or "")
            or not str(source.get("session_id") or "")
            or not str(source.get("parent_thread_id") or "")
        ):
            self._unknown("Codex provider source identity is malformed")
            return None
        root = Path(str(source.get("root") or ""))
        path = Path(str(source.get("path") or ""))
        try:
            root_resolved = root.resolve(strict=True)
            path_resolved = path.resolve(strict=True)
            if not path_resolved.is_relative_to(root_resolved) or path_resolved.suffix != ".jsonl":
                raise OSError("source path is outside the bound Codex session root")
        except (OSError, ValueError):
            self._unknown("Codex provider source path is unreadable or outside its bound root")
            return None
        parsed = _read_source(path_resolved)
        if parsed is None:
            self._unknown("Codex provider source cannot be read as structured JSONL")
            return None
        meta, lines = parsed
        if (
            str(meta.get("session_id") or "") != str(source.get("session_id") or "")
            or str(meta.get("cwd") or "") != str(Path(self.run.workspace).resolve(strict=False))
            or _parent_thread(lines) != str(source.get("parent_thread_id") or "")
        ):
            self._unknown("Codex provider source identity no longer matches this HeadRun")
            return None
        return meta, lines

    def _advance_cursor(self, source: Mapping[str, Any], line: SourceLine) -> None:
        self._replace_source({**dict(source), "cursor": {"line": line.number, "digest": line.digest}})

    def _run_at_cursor(self, source: Mapping[str, Any], line: SourceLine) -> HeadRun:
        policy = dict(self.run.fanout_policy)
        policy["provider_source"] = {
            **dict(source), "cursor": {"line": line.number, "digest": line.digest},
        }
        return self.run.with_fanout_policy(policy)

    def _replace_source(self, source: dict[str, Any]) -> None:
        policy = dict(self.run.fanout_policy)
        policy["provider_source"] = source
        updated = self.run.with_fanout_policy(policy)
        try:
            self.persist(updated)
        except Exception as exc:  # durable source binding/cursor loss is itself an unknown edge
            self._recording_failure(updated, {}, exc)
            return
        self.run = updated

    def _persist(self, run: HeadRun) -> None:
        self.persist(run)

    def _unknown(self, reason: str) -> None:
        policy = dict(self.run.fanout_policy)
        policy["state"] = "unknown"
        policy["terminal_state"] = FANOUT_TERMINAL_UNKNOWN
        policy["reason"] = reason
        updated = self.run.with_fanout_policy(policy)
        try:
            self.persist(updated)
        except Exception as exc:
            self._recording_failure(updated, {}, exc)
            return
        self.run = updated
        evidence = {"kind": "codex_provider_fanout", "state": "unknown", "reason": reason}
        try:
            self.stop(updated, reason)
        finally:
            self.block(evidence)
        raise CodexProviderSourceError(reason, run=updated)

    def _recording_failure(self, run: HeadRun, event: dict[str, Any], exc: Exception) -> None:
        failed = run.with_fanout_policy({
            **dict(run.fanout_policy),
            "state": "unknown",
            "terminal_state": FANOUT_TERMINAL_UNKNOWN,
            "reason": f"provider source could not be durably recorded: {type(exc).__name__}: {exc}",
        })
        self.run = failed
        evidence = {
            "kind": "codex_provider_fanout", "state": "unknown", "event": dict(event),
            "reason": failed.fanout_policy["reason"],
        }
        try:
            self.stop(failed, "provider source could not be durably recorded")
        finally:
            self.block(evidence)
        raise CodexFanoutRecordingError(failed.fanout_policy["reason"], run=failed, event=event)


def _read_source(path: Path) -> tuple[dict[str, Any], list[SourceLine]] | None:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    lines: list[SourceLine] = []
    meta: dict[str, Any] = {}
    for number, raw in enumerate(raw_lines, 1):
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        try:
            event: Any = json.loads(raw)
        except json.JSONDecodeError:
            event = _malformed(raw)
        lines.append(SourceLine(number=number, raw=raw, digest=digest, event=event))
        if not isinstance(event, dict) or event.get("type") != "session_meta" or meta:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        session_id = payload.get("session_id") or payload.get("id") or event.get("session_id")
        cwd = payload.get("cwd") or event.get("cwd")
        if isinstance(session_id, str) and isinstance(cwd, str):
            meta = {"session_id": session_id, "cwd": cwd}
    return meta, lines


def _parent_thread(lines: Iterable[SourceLine]) -> str:
    parents = [
        str(_event_view(line.event).get("thread_id") or "") for line in lines
        if _is_parent_started(_event_view(line.event))
    ]
    parents = [parent for parent in parents if parent]
    # The first ``thread.started`` is the parent established at source binding.  Later starts
    # are not allowed to rewrite that identity: ``_provider_events`` records them as unknown
    # relations during polling instead of making recovery attribute a child as the parent.
    return parents[0] if parents else ""


def _provider_events(
    event: Any, expected_parent: str, *, raw_event_digest: str
) -> Iterable[dict[str, Any]]:
    """Map one journal line to policy events without retaining its raw content.

    ``raw_event_digest`` is the SHA-256 of the exact JSONL line, rather than a digest of a
    normalized reconstruction.  The recorder accepts it as an internal transport field and
    persists only the digest in the terminal event record.
    """
    source = {"_secretary_raw_event_digest": raw_event_digest}
    if isinstance(event, dict) and event.get("_secretary_malformed_provider_event"):
        yield {
            "type": EVENT_UNPARSEABLE_PROVIDER_EVENT, "parent_thread_id": expected_parent,
            "provider_event": event, **source,
        }
        return
    if not isinstance(event, dict):
        yield {
            "type": EVENT_UNPARSEABLE_PROVIDER_EVENT, "parent_thread_id": expected_parent,
            "provider_event": event, **source,
        }
        return
    view = _event_view(event)
    if _is_parent_started(view):
        thread_id = str(view.get("thread_id") or "")
        if thread_id != expected_parent:
            yield {
                "type": EVENT_UNKNOWN_THREAD_EDGE, "parent_thread_id": thread_id,
                "provider_event": event, **source,
            }
        return
    item = view.get("item")
    if not isinstance(item, Mapping):
        return
    item_type = str(item.get("type") or "")
    # The retained Codex TUI journal uses ``CollabAgentToolCall`` inside
    # ``event_msg.payload.item``.  Keep the documented exec spelling too.  These are decoder
    # shapes only, never an allow-list for collaboration capability.
    if item_type not in {"collab_tool_call", "CollabAgentToolCall"}:
        if _collaboration_shaped(item_type):
            yield {
                "type": EVENT_UNKNOWN_THREAD_EDGE,
                "parent_thread_id": str(item.get("sender_thread_id") or view.get("thread_id") or ""),
                "tool": str(item.get("tool") or ""),
                "provider_event": event,
                **source,
            }
        return
    parent = str(item.get("sender_thread_id") or view.get("thread_id") or "")
    tool = str(item.get("tool") or "")
    receivers = item.get("receiver_thread_ids")
    if receivers is not None and not isinstance(receivers, list):
        yield {
            "type": EVENT_UNKNOWN_THREAD_EDGE,
            "parent_thread_id": parent,
            "tool": tool,
            "provider_event": event,
            **source,
        }
        return
    children = [str(child) for child in receivers if str(child)] if isinstance(receivers, list) else []
    if children:
        for child in children:
            yield {
                "type": EVENT_CHILD_THREAD_EDGE,
                "parent_thread_id": parent,
                "child_thread_id": child,
                "tool": tool,
                "provider_event": event, **source,
            }
        return
    yield {
        "type": EVENT_COLLABORATION_CALL,
        "parent_thread_id": parent,
        "tool": tool,
        "provider_event": event, **source,
    }


def _malformed(raw: str) -> dict[str, Any]:
    # Retain no event content.  The recorder records a SHA-256 digest of this fixed typed shape.
    return {"_secretary_malformed_provider_event": True, "length": len(raw)}


def _event_view(event: Any) -> Mapping[str, Any]:
    """Return the provider event inside Codex's persisted JSONL envelope.

    Codex journals each runtime event as an ``event_msg`` wrapper whose payload is the provider
    event.  Direct event objects remain accepted for the documented JSON event stream and the
    focused fixtures, but policy evidence always retains the digest of the original journal line.
    """
    if not isinstance(event, Mapping):
        return {}
    payload = event.get("payload")
    if event.get("type") == "event_msg" and isinstance(payload, Mapping):
        return payload
    return event


def _is_parent_started(event: Mapping[str, Any]) -> bool:
    return str(event.get("type") or "") in {"thread.started", "thread_started"}


def _collaboration_shaped(item_type: str) -> bool:
    """Whether an unrecognised item must be fenced rather than treated as ordinary output."""
    return "collab" in item_type.lower() or "agenttoolcall" in item_type.lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
