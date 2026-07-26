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
TUI_DELIVERY_RETRIES = int(os.environ.get("SECRETARY_TUI_DELIVERY_RETRIES", os.environ.get("TA_TUI_DELIVERY_RETRIES", "2")))
TUI_DELIVERY_TIMEOUT_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_TIMEOUT_S", os.environ.get("TA_TUI_DELIVERY_TIMEOUT_S", "12")))
TUI_DELIVERY_POLL_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_POLL_S", os.environ.get("TA_TUI_DELIVERY_POLL_S", "0.25")))
TUI_DELIVERY_RESEND_GRACE_S = float(os.environ.get("SECRETARY_TUI_DELIVERY_RESEND_GRACE_S", os.environ.get("TA_TUI_DELIVERY_RESEND_GRACE_S", "1")))
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_WORKING_RE = re.compile(r"\b(?:working|thinking)\b", re.IGNORECASE)


class TuiDeliveryError(RuntimeError):
    pass


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
    run_json([
        "orca", "terminal", "wait",
        "--terminal", handle,
        "--for", "tui-idle",
        "--timeout-ms", str(TUI_IDLE_TIMEOUT_MS),
        "--json",
    ])
    sent_at = time.time()
    run_json([
        "orca", "terminal", "send",
        "--terminal", handle,
        "--text", prompt,
        "--enter",
        "--json",
    ])
    _confirm_delivered(handle, workspace, prompt, sent_at, run_json=run_json, session_root=session_root)


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


def _screen_started_turn(screen: str) -> bool:
    marker = screen.rfind("\u203a")
    status_area = screen[:marker] if marker >= 0 else screen
    return bool(_WORKING_RE.search(status_area))
