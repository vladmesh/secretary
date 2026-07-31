"""Codex TUI prompt delivery for dispatcher-launched heads."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from secretary.dispatcher_launcher import CODEX_HOME_DEFAULT

RunJson = Callable[[list[str]], dict[str, Any]]

TUI_IDLE_TIMEOUT_MS = int(os.environ.get("SECRETARY_TUI_IDLE_TIMEOUT_MS", os.environ.get("TA_TUI_IDLE_TIMEOUT_MS", "60000")))
# Orca decides `tui-idle` from the pane's agent status and, failing that, from a quiescence window
# it polls. A probe shorter than that window would report every quiet pane as busy, so it is set
# above both rather than tuned for the fastest answer.
TUI_IDLE_PROBE_TIMEOUT_MS = int(os.environ.get("SECRETARY_TUI_IDLE_PROBE_TIMEOUT_MS", os.environ.get("TA_TUI_IDLE_PROBE_TIMEOUT_MS", "6000")))
TUI_DELIVERY_RETRIES = int(os.environ.get("SECRETARY_TUI_DELIVERY_RETRIES", os.environ.get("TA_TUI_DELIVERY_RETRIES", "2")))
TUI_DELIVERY_TIMEOUT_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_TIMEOUT_S", os.environ.get("TA_TUI_DELIVERY_TIMEOUT_S", "12")))
TUI_DELIVERY_POLL_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_POLL_S", os.environ.get("TA_TUI_DELIVERY_POLL_S", "0.25")))
TUI_DELIVERY_RESEND_GRACE_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_RESEND_GRACE_S", os.environ.get("TA_TUI_DELIVERY_RESEND_GRACE_S", "1")))
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CODEX_WORKING_RE = re.compile(r"\b(?:working|thinking)\b", re.IGNORECASE)
# Claude's composer has no Codex `›` marker. Its active turn is a dedicated status line, so a
# transcript word such as "working" or "thinking" is not enough to say a new turn started.
_CLAUDE_TURN_RE = re.compile(
    r"(?im)^\s*[✻✽✢✶·]\s+\S+?(?:…|\.\.\.)\s+\([^\n)]*\)\s*$"
)


class TuiDeliveryError(RuntimeError):
    pass


# What one delivery attempt achieved. `accepted` means the pane took the prompt into a turn while
# the caller's own proof of delivery is expected to arrive later, outside this call.
DELIVERY_CONFIRMED = "confirmed"
DELIVERY_ACCEPTED = "accepted"


def deliver_tui_prompt(
    handle: str,
    workspace: str,
    prompt_file: str,
    *,
    run_json: RunJson,
    session_root: Path | None = None,
    prompt_text: str | None = None,
) -> None:
    if prompt_text is not None:
        prompt = prompt_text
    else:
        try:
            prompt = (Path(workspace) / prompt_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise TuiDeliveryError(f"TUI prompt file is unreadable: {exc}") from None
    wait_for_tui_idle(handle, run_json=run_json)
    sent_at = time.time()
    run_json([
        "orca", "terminal", "send",
        "--terminal", handle,
        "--text", prompt,
        "--enter",
        "--json",
    ])
    _confirm_delivered(handle, workspace, prompt, sent_at, run_json=run_json, session_root=session_root)


def wait_for_tui_idle(handle: str, *, run_json: RunJson, timeout_ms: int | None = None) -> None:
    """Wait until Orca reports the pane ready for input. A refusal reaches the caller."""
    run_json([
        "orca", "terminal", "wait",
        "--terminal", handle,
        "--for", "tui-idle",
        "--timeout-ms", str(TUI_IDLE_TIMEOUT_MS if timeout_ms is None else timeout_ms),
        "--json",
    ])


def terminal_idle(handle: str, *, run_json: RunJson, timeout_ms: int | None = None) -> bool:
    """Whether the pane is ready for input right now, on Orca's signal rather than on its screen.

    This is the one readiness question the product asks about an interactive head, whatever
    provider runs in it: the runtime derives it from the pane's own agent status and falls back to
    a quiescence window. A pane Orca will not answer for, or one it reports as blocked behind a
    dialog, is deliberately not idle: an unreadable head stays live rather than being guessed
    quiet.
    """
    try:
        data = run_json([
            "orca", "terminal", "wait",
            "--terminal", handle,
            "--for", "tui-idle",
            "--timeout-ms", str(TUI_IDLE_PROBE_TIMEOUT_MS if timeout_ms is None else timeout_ms),
            "--json",
        ])
    except Exception:
        # A timeout is how Orca says "not idle", and it comes back as a failed command. Every other
        # refusal reads the same way here, because none of them is evidence of a quiet pane.
        return False
    wait = data.get("wait") if isinstance(data, dict) and isinstance(data.get("wait"), dict) else data
    if isinstance(wait, dict) and "satisfied" in wait:
        return bool(wait.get("satisfied"))
    return True


def terminal_turn_started(
    handle: str,
    *,
    run_json: RunJson,
    workspace: str = "",
    since: float = 0.0,
    adapter: str = "",
) -> bool:
    """Whether an interactive provider pane already accepted a prompt into a turn.

    Claude and Codex both persist their user turns locally. When recovery knows the delivery
    boundary, that durable record is the proof; the screen remains a secondary hint for old
    records that predate the boundary or for terminal-only recovery.
    """
    if workspace and since:
        if adapter == "claude":
            return bool(latest_claude_user_turn_for(workspace, since))
        if adapter == "codex":
            return bool(latest_user_turn_for(workspace, since))
    return _screen_started_turn(read_terminal_text(handle, run_json=run_json), adapter=adapter)


def deliver_interactive_prompt(
    handle: str,
    workspace: str,
    prompt: str,
    *,
    run_json: RunJson,
    confirm: Callable[[], bool] | None = None,
    ack_out_of_band: bool = False,
) -> str:
    """Deliver a prompt into a live interactive head, on one path for every role that has one.

    Terminal send succeeding only means Orca accepted keystrokes. Wait for the pane to be ready,
    send, then keep re-entering the prompt while the pane stays idle, which is what a swallowed
    prompt looks like. Exhausting the retries raises, so the caller can take its own failure path.

    What closes the delivery is the caller's business, not this function's. `confirm` defaults to
    the head's own durable user turn, which is the worker and reviewer criterion. A caller whose proof
    arrives later (an observer resume naming this delivery) passes that criterion here and sets
    `ack_out_of_band`, and gets `DELIVERY_ACCEPTED` as soon as the pane has taken the prompt.
    """
    wait_for_tui_idle(handle, run_json=run_json)
    sent_at = time.time()
    run_json([
        "orca", "terminal", "send",
        "--terminal", handle,
        "--text", prompt,
        "--enter",
        "--json",
    ])
    return _confirm_interactive_turn(
        handle,
        workspace,
        sent_at,
        run_json=run_json,
        confirm=confirm,
        ack_out_of_band=ack_out_of_band,
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
    """
    run_json(["orca", "terminal", "close", "--terminal", handle, "--json"])


def _confirm_delivered(
    handle: str,
    workspace: str,
    prompt: str,
    sent_at: float,
    *,
    run_json: RunJson,
    session_root: Path | None = None,
) -> None:
    deadline = time.monotonic() + TUI_DELIVERY_TIMEOUT_S
    next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
    resends = 0
    last_reason = "no-start-signal"
    while time.monotonic() < deadline:
        if latest_user_turn_for(workspace, sent_at, session_root=session_root):
            return
        screen = read_terminal_text(handle, run_json=run_json)
        if _screen_started_turn(screen):
            return
        if _prompt_still_in_codex_composer(screen, prompt):
            last_reason = "prompt-in-composer"
            if resends < TUI_DELIVERY_RETRIES and time.monotonic() >= next_resend_at:
                run_json([
                    "orca", "terminal", "send",
                    "--terminal", handle,
                    "--text", "",
                    "--enter",
                    "--json",
                ])
                resends += 1
                next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
        else:
            last_reason = "awaiting-start-signal"
        time.sleep(max(TUI_DELIVERY_POLL_S, 0.01))
    raise TuiDeliveryError(
        f"TUI prompt delivery was not confirmed after {TUI_DELIVERY_TIMEOUT_S:.1f}s "
        f"(reason={last_reason}, resends={resends})"
    )


def _confirm_interactive_turn(
    handle: str,
    workspace: str,
    sent_at: float,
    *,
    run_json: RunJson,
    confirm: Callable[[], bool] | None = None,
    ack_out_of_band: bool = False,
) -> str:
    confirmed = confirm or (lambda: bool(latest_claude_user_turn_for(workspace, sent_at)))
    deadline = time.monotonic() + TUI_DELIVERY_TIMEOUT_S
    next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
    resends = 0
    accepted = False
    while time.monotonic() < deadline:
        if confirmed():
            return DELIVERY_CONFIRMED
        if not terminal_idle(handle, run_json=run_json):
            # The pane went to work on something: the prompt is in, whether or not the caller's
            # own proof of it has appeared yet.
            accepted = True
            if ack_out_of_band:
                return DELIVERY_ACCEPTED
        elif resends < TUI_DELIVERY_RETRIES and time.monotonic() >= next_resend_at:
            accepted = False
            run_json([
                "orca", "terminal", "send",
                "--terminal", handle,
                "--text", "",
                "--enter",
                "--json",
            ])
            resends += 1
            next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
        time.sleep(max(TUI_DELIVERY_POLL_S, 0.01))
    raise TuiDeliveryError(
        f"interactive prompt delivery was not confirmed after {TUI_DELIVERY_TIMEOUT_S:.1f}s "
        f"(reason={'accepted-but-unconfirmed' if accepted else 'pane-stayed-idle'}, "
        f"resends={resends})"
    )


def read_terminal_text(handle: str, *, run_json: RunJson) -> str:
    data = run_json(["orca", "terminal", "read", "--terminal", handle, "--json"])
    terminal = data.get("terminal") if isinstance(data.get("terminal"), dict) else data
    if not isinstance(terminal, dict):
        return ""
    tail = terminal.get("tail")
    if isinstance(tail, list):
        return strip_ansi("\n".join(str(line) for line in tail))
    for key in ("text", "content", "screen"):
        value = terminal.get(key)
        if isinstance(value, str):
            return strip_ansi(value)
    return ""


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


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


def _claude_session_paths_for(workspace: str):
    root = _claude_projects_root()
    project = str(Path(workspace).resolve(strict=False)).replace("/", "-")
    try:
        yield from (root / project).glob("*.jsonl")
    except OSError:
        return


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


def _prompt_signature(prompt: str) -> str:
    for token in ("TASK.md", "REVIEW.md"):
        if token in prompt:
            return token
    words = re.findall(r"\S+", prompt)
    return " ".join(words[:6])


def _prompt_still_in_codex_composer(screen: str, prompt: str) -> bool:
    marker = screen.rfind("\u203a")
    if marker < 0:
        return False
    signature = _prompt_signature(prompt)
    return bool(signature and signature in screen[marker:])


def _screen_started_turn(screen: str, *, adapter: str = "") -> bool:
    if adapter == "claude":
        return bool(_CLAUDE_TURN_RE.search(screen))
    marker = screen.rfind("\u203a")
    status_area = screen[:marker] if marker >= 0 else screen
    return bool(_CODEX_WORKING_RE.search(status_area))
