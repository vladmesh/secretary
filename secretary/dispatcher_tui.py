"""Codex TUI prompt delivery for dispatcher-launched heads.

What to send and how to prove this role's head took it lives here; the delivery itself — readiness
classification, send, resend, failure — is `triggered_agents.runtime.tui_delivery`, the one path
every interactive head in the product goes through, service heads included.
"""

from __future__ import annotations

import json
import os
import re
import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from triggered_agents.runtime.codex_preflight import CODEX_HOME_DEFAULT
from triggered_agents.runtime.claude_sessions import (
    claude_project_dir_name,
    claude_session_paths,
)
from triggered_agents.runtime.head import HeadRun
from secretary.dispatcher_worker_lifecycle import (
    ContinuationProviderCondition,
    head_run_binding,
)
from triggered_agents.runtime.pane_host import OrcaSessionHost, PaneHost
from triggered_agents.runtime.tui_delivery import (
    COMPOSER_EMPTY,
    COMPOSER_UNKNOWN,
    DELIVERY_ACCEPTED,
    DELIVERY_CONFIRMED,
    READINESS_BLOCKED,
    READINESS_BUSY,
    READINESS_READY,
    READINESS_STALE_HANDLE,
    READINESS_UNKNOWN,
    READINESS_UNAVAILABLE,
    STAGE_ACKNOWLEDGED,
    STAGE_ENTER_ACCEPTED,
    STAGE_PAYLOAD_WRITTEN,
    STAGE_TURN_OBSERVED,
    DeliveryEvidence,
    DeliveryOutcome,
    RunJson,
    TuiDeliveryError,
    composer_fingerprint,
    deliver_interactive_prompt,
    delivery_readiness_state,
    output_cursor,
    read_pane_text,
    strip_ansi,
    terminal_readiness,
    wait_for_tui_idle,
)

__all__ = [
    "COMPOSER_EMPTY",
    "COMPOSER_UNKNOWN",
    "DELIVERY_ACCEPTED",
    "DELIVERY_CONFIRMED",
    "READINESS_BLOCKED",
    "READINESS_BUSY",
    "READINESS_READY",
    "READINESS_STALE_HANDLE",
    "READINESS_UNKNOWN",
    "READINESS_UNAVAILABLE",
    "STAGE_ACKNOWLEDGED",
    "STAGE_ENTER_ACCEPTED",
    "STAGE_PAYLOAD_WRITTEN",
    "STAGE_TURN_OBSERVED",
    "DeliveryEvidence",
    "DeliveryOutcome",
    "RunJson",
    "TuiDeliveryError",
    "claude_project_dir_name",
    "close_terminal",
    "close_terminal_strict",
    "composer_fingerprint",
    "deliver_interactive_prompt",
    "delivery_readiness_state",
    "deliver_tui_prompt",
    "latest_claude_user_turn_for",
    "bind_claude_provider_progress_source",
    "prepare_claude_provider_progress_source",
    "provider_progress_for_run",
    "latest_user_turn_for",
    "output_cursor",
    "read_terminal_text",
    "strip_ansi",
    "terminal_readiness",
    "terminal_turn_started",
    "turn_started_confirm",
    "wait_for_tui_idle",
]

_CODEX_WORKING_RE = re.compile(r"\b(?:working|thinking)\b", re.IGNORECASE)
# Claude's composer has no Codex `›` marker. Its active turn is a dedicated status line, so a
# transcript word such as "working" or "thinking" is not enough to say a new turn started.
#
# What is stable about that line across versions is its shape: some decoration, then a capitalised
# gerund closed by an ellipsis — `Forming...`, `Thinking…`, `Tempering…`, `Bloviating…`. What is not
# stable is anything else about it. The previous pattern pinned the spinner to `[✻✽✢✶·]` and
# required a parenthesised `(4s · ↑ 13.2k tokens)` suffix; Claude 2.1.227 cycles `●` through that
# spinner too, and the line Orca's pane read returns is an overlay of the alternate screen —
# `· Tempering…e /btw to ask a 9u ck side question…` — in which the suffix is painted over. So the
# pattern stopped matching anything at all, and the last screen-side hint died on a version bump.
# It is only a hint: what proves a Claude turn is the user record the provider persists, and the
# roles that use this never let a screen that says nothing decide the fate of a pane.
_CLAUDE_TURN_RE = re.compile(r"(?m)^[^\w\n]*[A-Z][A-Za-z]*(?:…|\.\.\.)")


def deliver_tui_prompt(
    handle: str,
    workspace: str,
    prompt_file: str,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    adapter: str = "codex",
    session_root: Path | None = None,
    prompt_text: str | None = None,
    subject: str = "",
    document_path: str = "",
    before_send: Callable[[], None] | None = None,
) -> DeliveryOutcome:
    """Deliver one provider TUI prompt through the shared transport and confirmation path.

    Nothing here is a second delivery path. It resolves what to send, which is the caller's
    business, and hands the same criterion worker and reviewer use on any other head: their head's
    turn having visibly started.

    A caller that has already written its task to a document passes that document's path with the
    nudge it wants sent, so the evidence records which of the two the pane received.

    The pane is reached through `host` when the caller has one and through its own runner when it
    does not. A head operation always has one: that is what keeps the product's delivery — this
    criterion included — inside the session-manager seam instead of beside it.
    """
    if prompt_text is not None:
        prompt = prompt_text
    else:
        try:
            prompt = (Path(workspace) / prompt_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise TuiDeliveryError(f"TUI prompt file is unreadable: {exc}") from None
    return deliver_interactive_prompt(
        handle,
        prompt,
        run_json=run_json,
        host=host,
        adapter=adapter,
        confirm=turn_started_confirm(
            handle, workspace, adapter, run_json=run_json, host=host, session_root=session_root
        ),
        subject=subject,
        document_path=document_path,
        before_send=before_send,
    )


def turn_started_confirm(
    handle: str,
    workspace: str,
    adapter: str,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    session_root: Path | None = None,
) -> Callable[[float], bool]:
    """The worker and reviewer delivery criterion, on whichever head that role was given.

    Their providers persist a user turn locally, and that record after the send boundary is the
    first proof; a pane showing a turn underway is the second. This is the criterion the roles
    have always used, expressed as something a caller passes rather than something the delivery
    path decides for them.
    """
    def confirm(sent_at: float) -> bool:
        if terminal_turn_started(
            handle,
            run_json=run_json,
            host=host,
            workspace=workspace,
            since=sent_at,
            adapter=adapter,
            session_root=session_root,
        ):
            return True
        return terminal_turn_started(handle, run_json=run_json, host=host, adapter=adapter)

    return confirm


def terminal_turn_started(
    handle: str,
    *,
    run_json: RunJson | None = None,
    host: PaneHost | None = None,
    workspace: str = "",
    since: float = 0.0,
    adapter: str = "",
    session_root: Path | None = None,
) -> bool:
    """Whether an interactive provider pane already accepted a prompt into a turn.

    Claude and Codex both persist their user turns locally. When recovery knows the delivery
    boundary, that durable record is the proof; the screen remains a secondary hint for old
    records that predate the boundary or for terminal-only recovery.

    Secondary in both directions. A screen that shows a turn underway is worth believing, and a
    screen that shows nothing is worth nothing: a repainting TUI read through a pane snapshot can
    be between frames, in an alternate-screen overlay, or drawing a status line this pattern has
    never seen. No caller may take the `False` this returns as proof that a head did not get its
    prompt, and none does — an unproven delivery hands its pane back instead of closing it.
    """
    if workspace and since:
        if adapter == "claude":
            return bool(latest_claude_user_turn_for(workspace, since))
        if adapter == "codex":
            return bool(latest_user_turn_for(workspace, since, session_root=session_root))
    return _screen_started_turn(
        read_terminal_text(handle, run_json=run_json, host=host), adapter=adapter
    )


def close_terminal(handle: str, *, run_json: RunJson) -> None:
    try:
        close_terminal_strict(handle, run_json=run_json)
    except Exception:
        pass


def close_terminal_strict(handle: str, *, run_json: RunJson) -> None:
    """Close a terminal and let a refusal reach the caller.

    Cleanup paths swallow the failure because they already have one to report. A caller whose
    record is the only pointer to the pane cannot: a refused close leaves the head alive.

    The reviewer's lifecycle paths are the callers left here; the worker's close is the head
    operation's, through the session host it was given (secretary-1412).
    """
    OrcaSessionHost(run_json).close_pane(handle)


def read_terminal_text(
    handle: str, *, run_json: RunJson | None = None, host: PaneHost | None = None
) -> str:
    """The pane's text, read the one way the delivery boundary reads it."""
    return read_pane_text(handle, run_json=run_json, host=host)


def latest_user_turn_for(
    workspace: str,
    since: float,
    *,
    session_root: Path | None = None,
) -> float | None:
    latest: float | None = None
    for path in _session_paths_for(workspace, session_root=session_root):
        try:
            with path.open(encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict) or not _is_user_turn(record):
                        continue
                    timestamp = _record_timestamp(record)
                    if timestamp is None or timestamp <= since:
                        continue
                    if latest is None or timestamp > latest:
                        latest = timestamp
        except OSError:
            continue
    return latest


def latest_claude_user_turn_for(workspace: str, since: float) -> float | None:
    """Return the newest Claude user record for this workspace after ``since``."""
    latest: float | None = None
    for path in _claude_session_paths_for(workspace):
        try:
            with path.open(encoding="utf-8", errors="replace") as source:
                for line in source:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict) or record.get("type") != "user":
                        continue
                    timestamp = _record_timestamp(record)
                    if timestamp is None or timestamp <= since:
                        continue
                    if latest is None or timestamp > latest:
                        latest = timestamp
        except OSError:
            continue
    return latest


def prepare_claude_provider_progress_source(run: HeadRun) -> HeadRun:
    """Persist the pre-pane Claude baseline for this exact HeadRun.

    The provider journal does not carry a Secretary run id.  Selecting a transcript therefore has
    to start with the files that existed before this run's pane was opened.  A missing root is
    still a durable unavailable source, rather than permission to inspect the workspace later.
    """
    if run.spec.adapter != "claude":
        return run
    run_id, run_fingerprint = head_run_binding(run.to_json())
    root = _claude_projects_root()
    baseline: list[str] = []
    if root.is_dir():
        try:
            baseline = sorted(
                str(path.resolve(strict=False))
                for path in claude_session_paths(run.workspace, root=root)
                if path.is_file()
            )
        except OSError:
            baseline = []
    policy = dict(run.fanout_policy)
    policy["provider_progress_source"] = {
        "version": 1,
        "kind": "claude_session_jsonl",
        # Claude may create its projects root only when the pane first starts.  The empty
        # pre-pane baseline is still a real launch baseline; later binding accepts exactly one
        # new transcript beneath this recorded root and never scans another location.
        "state": "unbound",
        "reason": "",
        "run_id": run_id,
        "head_run_fingerprint": run_fingerprint,
        "workspace": str(Path(run.workspace).resolve(strict=False)),
        "role": run.role,
        "task_ref": run.task_ref.to_json(),
        "root": str(root.resolve(strict=False)),
        "baseline": baseline,
    }
    return run.with_fanout_policy(policy)


def bind_claude_provider_progress_source(run: HeadRun) -> HeadRun:
    """Bind one newly-created Claude transcript without choosing a same-workspace fallback."""
    source = _progress_source(run)
    if source.get("kind") != "claude_session_jsonl" or source.get("state") != "unbound":
        return run
    reason = _source_matches_run(source, run)
    if reason:
        return _replace_progress_source(run, {**source, "state": "unavailable", "reason": reason})
    root = Path(str(source.get("root") or ""))
    baseline = source.get("baseline")
    if not root.is_dir() or not isinstance(baseline, list) or not all(isinstance(path, str) for path in baseline):
        return _replace_progress_source(run, {
            **source, "state": "unavailable", "reason": "Claude source baseline is unavailable or malformed",
        })
    try:
        root_resolved = root.resolve(strict=True)
        candidates = [
            path.resolve(strict=True)
            for path in claude_session_paths(run.workspace, root=root)
            if str(path.resolve(strict=False)) not in set(baseline)
        ]
        candidates = [path for path in candidates if path.is_relative_to(root_resolved)]
    except (OSError, ValueError):
        return _replace_progress_source(run, {
            **source, "state": "unavailable", "reason": "Claude source cannot be enumerated",
        })
    if len(candidates) != 1:
        return _replace_progress_source(run, {
            **source,
            "state": "unavailable",
            "reason": "Claude source is ambiguous or absent after launch",
        })
    try:
        stat = candidates[0].stat()
    except OSError:
        return _replace_progress_source(run, {
            **source, "state": "unavailable", "reason": "Claude source cannot be inspected",
        })
    return _replace_progress_source(run, {
        **source,
        "state": "bound",
        "path": str(candidates[0]),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "initial_size": int(stat.st_size),
        "initial_mtime_ns": int(stat.st_mtime_ns),
    })


def provider_progress_for_run(run: HeadRun) -> dict[str, str]:
    """Resolve one exact run-bound provider source, never a workspace activity maximum."""
    run_json = run.to_json()
    run_id, fingerprint = head_run_binding(run_json)
    if not run_id:
        return {"state": "unavailable", "reason": "persisted HeadRun is malformed"}
    if run.spec.adapter == "codex":
        return _codex_provider_progress_for_run(run, run_id, fingerprint)
    if run.spec.adapter == "claude":
        return _claude_provider_progress_for_run(run, run_id, fingerprint)
    return {"state": "unavailable", "reason": "provider adapter has no progress source contract"}


def _codex_provider_progress_for_run(run: HeadRun, run_id: str, fingerprint: str) -> dict[str, str]:
    from secretary.codex_provider_events import _initial_range_matches, _read_source

    source = run.fanout_policy.get("provider_source")
    if not isinstance(source, dict):
        return _unavailable("Codex provider source has no bound v1 baseline", "codex-session")
    if source.get("state") == "unbound":
        if _legacy_unbound_v1_codex_source(source, run):
            return _unavailable(
                "Codex provider source has no bound v1 baseline",
                "codex-session",
                continuation_condition=ContinuationProviderCondition.LEGACY_UNBOUND_V1,
            )
        return _unavailable("Codex provider source has no bound v1 baseline", "codex-session")
    if source.get("state") != "bound":
        return _unavailable("Codex provider source has no bound v1 baseline", "codex-session")
    reason = _source_matches_run(source, run)
    if reason:
        return _unavailable(reason, "codex-session", identity=True)
    root = Path(str(source.get("root") or ""))
    path = Path(str(source.get("path") or ""))
    try:
        root_resolved = root.resolve(strict=True)
        path_resolved = path.resolve(strict=True)
        if not path_resolved.is_relative_to(root_resolved) or path_resolved.suffix != ".jsonl":
            raise OSError
        parsed = _read_source(path_resolved)
        if parsed is None or not _initial_range_matches(source, parsed[1]):
            raise OSError
        stat = path_resolved.stat()
    except (OSError, ValueError):
        return _unavailable("Codex bound provider source cannot be verified", "codex-session")
    meta, lines = parsed
    if (
        str(meta.get("session_id") or "") != str(source.get("session_id") or "")
        or not lines
    ):
        return _unavailable("Codex bound provider source identity changed", "codex-session", identity=True)
    cursor = f"{lines[-1].number}:{lines[-1].digest}"
    return _observed(
        "codex-session", _source_fingerprint(source), cursor, stat.st_mtime,
        run_id, fingerprint,
    )


def _claude_provider_progress_for_run(run: HeadRun, run_id: str, fingerprint: str) -> dict[str, str]:
    source = _progress_source(run)
    if source.get("kind") != "claude_session_jsonl" or source.get("state") != "bound":
        return _unavailable("Claude provider source has no bound v1 baseline", "claude-session")
    reason = _source_matches_run(source, run)
    if reason:
        return _unavailable(reason, "claude-session", identity=True)
    root = Path(str(source.get("root") or ""))
    path = Path(str(source.get("path") or ""))
    try:
        root_resolved = root.resolve(strict=True)
        path_resolved = path.resolve(strict=True)
        stat = path_resolved.stat()
        allowed = {candidate.resolve(strict=True) for candidate in claude_session_paths(run.workspace, root=root)}
        if (
            not path_resolved.is_relative_to(root_resolved)
            or path_resolved not in allowed
            or int(source.get("device")) != int(stat.st_dev)
            or int(source.get("inode")) != int(stat.st_ino)
            or int(source.get("initial_size")) > int(stat.st_size)
        ):
            raise OSError
    except (OSError, TypeError, ValueError):
        return _unavailable("Claude bound provider source cannot be verified", "claude-session")
    cursor = f"{int(stat.st_size)}:{int(stat.st_mtime_ns)}"
    return _observed(
        "claude-session", _source_fingerprint(source), cursor, stat.st_mtime,
        run_id, fingerprint,
    )


def _progress_source(run: HeadRun) -> dict[str, Any]:
    source = run.fanout_policy.get("provider_progress_source")
    return dict(source) if isinstance(source, dict) else {}


def _replace_progress_source(run: HeadRun, source: dict[str, Any]) -> HeadRun:
    policy = dict(run.fanout_policy)
    policy["provider_progress_source"] = source
    return run.with_fanout_policy(policy)


def _source_matches_run(source: dict[str, Any], run: HeadRun) -> str:
    run_id, fingerprint = head_run_binding(run.to_json())
    if (
        source.get("version") != 1
        or str(source.get("run_id") or "") != run_id
        or str(source.get("head_run_fingerprint") or "") != fingerprint
        or str(source.get("workspace") or "") != str(Path(run.workspace).resolve(strict=False))
        or str(source.get("role") or "") != run.role
        or source.get("task_ref") != run.task_ref.to_json()
    ):
        return "provider source binding does not match this HeadRun"
    return ""


def _source_fingerprint(source: dict[str, Any]) -> str:
    stable = {
        key: source.get(key) for key in (
            "version", "kind", "run_id", "head_run_fingerprint", "workspace", "role",
            "task_ref", "root", "path", "session_id", "parent_thread_id", "initial_range",
            "device", "inode", "initial_size", "initial_mtime_ns",
        )
    }
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()[:32]


def _observed(
    source: str, source_fingerprint: str, cursor: str, observed_at: float,
    run_id: str, run_fingerprint: str,
) -> dict[str, str]:
    return {
        "state": "observed",
        "admission": "accepted",
        "source": source,
        "source_fingerprint": source_fingerprint,
        "cursor": cursor,
        "observed_at": str(float(observed_at)),
        "head_run_id": run_id,
        "head_run_fingerprint": run_fingerprint,
    }


def _legacy_unbound_v1_codex_source(source: dict[str, Any], run: HeadRun) -> bool:
    """Whether ``source`` is the exact v1 preflight descriptor left unbound on a legacy run.

    This deliberately checks structure and the HeadRun fence rather than a diagnostic string.  An
    unbound descriptor whose baseline is malformed, or whose immutable fields belong to another
    run, remains ordinary unavailable/identity evidence and cannot start a replacement.
    """
    return (
        run.spec.adapter == "codex"
        and run.fanout_policy.get("provider_source_required") is True
        and source.get("version") == 1
        and source.get("kind") == "codex_session_event_jsonl"
        and source.get("state") == "unbound"
        and isinstance(source.get("root"), str)
        and bool(source.get("root"))
        and isinstance(source.get("baseline"), list)
        and all(isinstance(path, str) for path in source["baseline"])
        and not _source_matches_run(source, run)
    )


def _unavailable(
    reason: str,
    source: str,
    *,
    identity: bool = False,
    continuation_condition: ContinuationProviderCondition | None = None,
) -> dict[str, str]:
    result = {
        "state": "identity_mismatch" if identity else "unavailable",
        "source": source,
        "reason": reason,
    }
    if continuation_condition is not None:
        result["continuation_condition"] = continuation_condition.value
    return result


def _claude_session_paths_for(workspace: str):
    return claude_session_paths(workspace, root=_claude_projects_root())


def _claude_projects_root() -> Path:
    configured = os.environ.get("SECRETARY_CLAUDE_PROJECTS") or os.environ.get("TA_CLAUDE_PROJECTS")
    return Path(configured) if configured else Path.home() / ".claude" / "projects"


def _session_paths_for(workspace: str, *, session_root: Path | None = None):
    root = session_root or _sessions_root()
    if not root.is_dir():
        return
    wanted = str(Path(workspace).resolve(strict=False))
    for day_dir in _recent_day_dirs(root):
        try:
            files = list(day_dir.glob("*.jsonl"))
        except OSError:
            continue
        for path in files:
            if _session_cwd(path) == wanted:
                yield path


def _sessions_root() -> Path:
    root = os.environ.get("SECRETARY_CODEX_SESSIONS") or os.environ.get("TA_CODEX_SESSIONS")
    if root:
        return Path(root)
    home = os.environ.get("TA_CODEX_HOME") or CODEX_HOME_DEFAULT
    return Path(home) / "sessions"


def _session_cwd(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            for index, line in enumerate(source):
                if index >= 20:
                    return None
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                cwd = payload.get("cwd") or record.get("cwd")
                return cwd if isinstance(cwd, str) and cwd else None
    except OSError:
        return None
    return None


def _recent_day_dirs(root: Path, limit: int = 2) -> list[Path]:
    try:
        years = sorted((path for path in root.iterdir() if path.is_dir() and path.name.isdigit()), key=lambda path: path.name, reverse=True)
    except OSError:
        return [root]
    if not years:
        return [root]
    days: list[Path] = []
    for year in years:
        try:
            months = sorted((path for path in year.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True)
        except OSError:
            continue
        for month in months:
            try:
                leaves = sorted((path for path in month.iterdir() if path.is_dir()), key=lambda path: path.name, reverse=True)
            except OSError:
                continue
            for day in leaves:
                days.append(day)
                if len(days) >= limit:
                    return days
    return days or [root]


def _record_timestamp(record: dict[str, Any]) -> float | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_user_turn(record: dict[str, Any]) -> bool:
    payload = record.get("payload")
    return (
        isinstance(payload, dict)
        and record.get("type") == "event_msg"
        and payload.get("type") == "user_message"
    )


def _screen_started_turn(screen: str, *, adapter: str = "") -> bool:
    if adapter == "claude":
        return bool(_CLAUDE_TURN_RE.search(screen))
    marker = screen.rfind("\u203a")
    status_area = screen[:marker] if marker >= 0 else screen
    return bool(_CODEX_WORKING_RE.search(status_area))
