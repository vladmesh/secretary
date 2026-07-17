"""Terminal handle contracts for pipeline-owned head sessions."""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from typing import Any

OrcaJSON = Callable[[list[str]], dict[str, Any]]
ReadTerminalText = Callable[[str], str]

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SHELL_PROMPT_RE = re.compile(
    r"(?:^|\n)\s*(?:\([^)\n]+\)\s*)?(?:[\w.-]+@[\w.-]+:\s*)?"
    r"(?:~|/|[A-Za-z]:\\)[^\n]{0,240}[$#%]\s*$"
)
_CODEX_TUI_PROMPT_RE = re.compile(
    r"\u203a[^\n]*(?:gpt|o\d|codex)[A-Za-z0-9_.-]*"
    r"(?:\s+(?:default|low|medium|high|xhigh))?\s+\u00b7\s+(?:~|/|[A-Za-z]:\\)"
)
_CODEX_TUI_MODEL_SETTING_RE = re.compile(
    r"(?m)^\s*(?:[\u2502|]\s*)?model:\s*(?:gpt|o\d|codex)[A-Za-z0-9_.-]*"
    r"(?:\s+(?:default|low|medium|high|xhigh))?\b"
)
_CODEX_TUI_PERMISSION_RE = re.compile(r"(?m)^\s*(?:[\u2502|]\s*)?permissions:\s+\S+")
_CODEX_TUI_HEADER_RE = re.compile(r"\bOpenAI Codex(?:\s+\(v[0-9A-Za-z_.-]+\))?\b")
_CODEX_TUI_FRAME_RE = re.compile(r"[\u2500\u2502\u250c\u2510\u2514\u2518]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def entry_dead_reason(term: dict[str, Any]) -> str | None:
    if term.get("connected") is False:
        return "disconnected"
    if term.get("writable") is False:
        return "unwritable"
    preview = strip_ansi(str(term.get("preview") or "")).strip()
    if preview and _SHELL_PROMPT_RE.search(preview):
        return "shell-prompt"
    return None


def entry_live(term: dict[str, Any]) -> bool:
    return entry_dead_reason(term) is None


def entry_text(term: dict[str, Any]) -> str:
    parts = []
    for key in ("title", "preview"):
        value = term.get(key)
        if value:
            parts.append(str(value))
    return strip_ansi("\n".join(parts))


def looks_like_codex_tui(text: str) -> bool:
    text = strip_ansi(text)
    if _CODEX_TUI_PROMPT_RE.search(text):
        return True
    return bool(
        _CODEX_TUI_HEADER_RE.search(text)
        and _CODEX_TUI_MODEL_SETTING_RE.search(text)
        and _CODEX_TUI_PERMISSION_RE.search(text)
        and _CODEX_TUI_FRAME_RE.search(text)
    )


def read_terminal_text(handle: str, orca_json: OrcaJSON) -> str:
    data = orca_json(["terminal", "read", "--terminal", handle, "--limit", "120"])
    term = data.get("terminal", data)
    return strip_ansi("\n".join(str(line) for line in (term.get("tail") or [])))


def expected_kind_dead_reason(
    term: dict[str, Any],
    expected_kind: str | None,
    handle: str,
    *,
    read_terminal_text: ReadTerminalText,
    unavailable_errors: tuple[type[BaseException], ...] = (subprocess.TimeoutExpired,),
) -> str | None:
    if not expected_kind:
        return None
    if expected_kind != "codex-tui":
        return "unknown-terminal-kind"
    if looks_like_codex_tui(entry_text(term)):
        return None
    try:
        if handle and looks_like_codex_tui(read_terminal_text(handle)):
            return None
    except unavailable_errors:
        pass
    return "not-codex-tui"


def belongs_to_workspace(term: dict[str, Any], workspace: str) -> bool:
    candidates = []

    def add_candidate(value, explicit_path: bool = False) -> None:
        if isinstance(value, dict):
            candidates.extend(value.get(k) for k in ("path", "root", "worktreePath", "workspacePath"))
            return
        if not value:
            return
        text = str(value)
        path_like = (
            explicit_path
            or text.startswith(("path:", "~", "/"))
            or "::" in text
            or bool(re.match(r"^[A-Za-z]:\\", text))
        )
        if path_like:
            candidates.append(value)

    for key in ("worktreePath", "workspacePath"):
        add_candidate(term.get(key), explicit_path=True)
    for key in ("worktree", "workspace"):
        add_candidate(term.get(key))
    candidates = [c for c in candidates if c]
    if not candidates:
        return True

    def norm(value) -> str:
        text = str(value)
        if "::" in text:
            text = text.split("::", 1)[-1]
        if text.startswith("path:"):
            text = text[len("path:"):]
        return os.path.abspath(os.path.expanduser(text))

    want = norm(workspace)
    return any(norm(c) == want for c in candidates)


def terminal_status(
    handle: str,
    workspace: str | None = None,
    expected_kind: str | None = None,
    *,
    orca_json: OrcaJSON,
    unavailable_errors: tuple[type[BaseException], ...] = (subprocess.TimeoutExpired,),
) -> dict[str, Any]:
    if not handle:
        return {"known": True, "live": False, "reason": "missing-handle"}
    args = ["terminal", "list", "--limit", "50"]
    if workspace:
        args.extend(["--worktree", f"path:{workspace}"])
    try:
        data = orca_json(args)
    except unavailable_errors:
        return {"known": False, "live": False, "reason": "terminal-list-unavailable"}
    for term in data.get("terminals") or []:
        if (term.get("handle") or term.get("id")) != handle:
            continue
        if workspace and not belongs_to_workspace(term, workspace):
            return {"known": True, "live": False, "reason": "wrong-workspace"}
        reason = entry_dead_reason(term)
        if reason:
            return {"known": True, "live": False, "reason": reason}
        reason = expected_kind_dead_reason(
            term,
            expected_kind,
            handle,
            read_terminal_text=lambda terminal: read_terminal_text(terminal, orca_json),
            unavailable_errors=unavailable_errors,
        )
        if reason:
            return {"known": True, "live": False, "reason": reason}
        last = term.get("lastOutputAt")
        last_activity = None
        if last:
            try:
                last_activity = float(last) / 1000.0
            except (TypeError, ValueError):
                last_activity = None
        return {"known": True, "live": True, "reason": "live", "last_activity": last_activity}
    return {"known": True, "live": False, "reason": "missing-terminal"}


def terminal_live(
    handle: str,
    workspace: str | None = None,
    expected_kind: str | None = None,
    *,
    orca_json: OrcaJSON,
    unavailable_errors: tuple[type[BaseException], ...] = (subprocess.TimeoutExpired,),
) -> bool:
    return bool(
        terminal_status(
            handle,
            workspace,
            expected_kind,
            orca_json=orca_json,
            unavailable_errors=unavailable_errors,
        ).get("live")
    )
