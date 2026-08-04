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
_WAIT_ERROR_CODE_RE = re.compile(r'"code"\s*:\s*"([a-z_]+)"')
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

# What Orca answered about a pane. `blocked` is a pane held in a dialog: not ready for a prompt,
# and not working on one either. `unknown` is the probe failing, which is not a busy head.
READINESS_READY = "ready"
READINESS_BUSY = "busy"
READINESS_BLOCKED = "blocked"
READINESS_UNKNOWN = "unknown"


def deliver_tui_prompt(
    handle: str,
    workspace: str,
    prompt_file: str,
    *,
    run_json: RunJson,
    session_root: Path | None = None,
    prompt_text: str | None = None,
) -> None:
    """Deliver a Codex TUI prompt: the shared path, with this role's own criterion.

    Nothing here is a second delivery path. It resolves what to send, which is the caller's
    business, and hands the same criterion worker and reviewer use on any other head: their head's
    turn having visibly started.
    """
    if prompt_text is not None:
        prompt = prompt_text
    else:
        try:
            prompt = (Path(workspace) / prompt_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise TuiDeliveryError(f"TUI prompt file is unreadable: {exc}") from None
    deliver_interactive_prompt(
        handle,
        prompt,
        run_json=run_json,
        confirm=turn_started_confirm(
            handle, workspace, "codex", run_json=run_json, session_root=session_root
        ),
    )


def turn_started_confirm(
    handle: str,
    workspace: str,
    adapter: str,
    *,
    run_json: RunJson,
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
            workspace=workspace,
            since=sent_at,
            adapter=adapter,
            session_root=session_root,
        ):
            return True
        return terminal_turn_started(handle, run_json=run_json, adapter=adapter)

    return confirm


def wait_for_tui_idle(handle: str, *, run_json: RunJson, timeout_ms: int | None = None) -> None:
    """Wait until Orca reports the pane ready for input. A refusal reaches the caller."""
    run_json([
        "orca", "terminal", "wait",
        "--terminal", handle,
        "--for", "tui-idle",
        "--timeout-ms", str(TUI_IDLE_TIMEOUT_MS if timeout_ms is None else timeout_ms),
        "--json",
    ])


def terminal_readiness(handle: str, *, run_json: RunJson, timeout_ms: int | None = None) -> str:
    """Ask Orca whether the pane is ready for input, and answer in three states, not two.

    This is the one readiness question the product asks about an interactive head, whatever
    provider runs in it: the runtime derives it from the pane's own agent status and falls back to
    a quiescence window, so no screen is read here.

    `READINESS_BUSY` is the condition not being met by a pane that is working, which Orca reports
    as a satisfied-false answer or as a failed command carrying `code: timeout`. A pane it names a
    `blockedReason` for is `READINESS_BLOCKED`: also not ready, but held in a dialog rather than
    working, so a prompt sent to it went nowhere. `READINESS_UNKNOWN` is the probe itself failing,
    and it must not be read as an ordinary busy head: a caller that cannot ask the question is not
    looking at a working observer, it is looking at nothing.
    """
    try:
        data = run_json([
            "orca", "terminal", "wait",
            "--terminal", handle,
            "--for", "tui-idle",
            "--timeout-ms", str(TUI_IDLE_PROBE_TIMEOUT_MS if timeout_ms is None else timeout_ms),
            "--json",
        ])
    except Exception as exc:
        return _refused_wait_readiness(exc)
    wait = data.get("wait") if isinstance(data, dict) and isinstance(data.get("wait"), dict) else data
    if isinstance(wait, dict) and "satisfied" in wait:
        return _answered_readiness(wait)
    return READINESS_READY


def _refused_wait_readiness(exc: Exception) -> str:
    """Classify a `terminal wait` the host refused, from the body Orca printed with it.

    The CLI exits non-zero both for a condition it could not satisfy and for a failure, and the
    host turns the two into the same exception, so the answer is in the text rather than in the
    outcome. It prints that text as JSON, and the host carries it into the failure it raises:

      * a `wait` object saying `satisfied: false` is a pane Orca has looked at and found working
        or blocked behind a dialog. That is busy, and busy waits for readiness;
      * `code: timeout` is the same condition not being met before the probe's own deadline;
      * anything else, a body that cannot be read included, is a probe that was never answered.
    """
    body = _json_object(str(exc))
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    wait = result.get("wait") if isinstance(result, dict) else None
    if isinstance(wait, dict) and "satisfied" in wait:
        return _answered_readiness(wait)
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(error.get("code") or "")
    if not code:
        # A body too damaged to parse can still carry its code in the text.
        codes = _WAIT_ERROR_CODE_RE.findall(str(exc))
        code = codes[-1] if codes else ""
    return READINESS_BUSY if code == "timeout" else READINESS_UNKNOWN


def _answered_readiness(wait: dict[str, Any]) -> str:
    if wait.get("satisfied"):
        return READINESS_READY
    return READINESS_BLOCKED if wait.get("blockedReason") else READINESS_BUSY


def _json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        return {}
    try:
        parsed = json.loads(text[start:])
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def terminal_turn_started(
    handle: str,
    *,
    run_json: RunJson,
    workspace: str = "",
    since: float = 0.0,
    adapter: str = "",
    session_root: Path | None = None,
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
            return bool(latest_user_turn_for(workspace, since, session_root=session_root))
    return _screen_started_turn(read_terminal_text(handle, run_json=run_json), adapter=adapter)


def deliver_interactive_prompt(
    handle: str,
    prompt: str,
    *,
    run_json: RunJson,
    confirm: Callable[[float], bool] | None = None,
    ack_out_of_band: bool = False,
) -> str:
    """Deliver a prompt into a live interactive head, on one path for every role that has one.

    Terminal send succeeding only means Orca accepted keystrokes. Wait for the pane to be ready,
    send, then keep re-entering the prompt while the pane stays idle, which is what a swallowed
    prompt looks like. Exhausting the retries raises, so the caller can take its own failure path.

    Worker and reviewer pass `confirm`, the criterion they always had: their head's turn having
    visibly started. A caller whose proof arrives later sets `ack_out_of_band` and passes no
    callback at all; it gets `DELIVERY_ACCEPTED` as soon as the pane has taken the prompt.
    """
    if confirm is None and not ack_out_of_band:
        raise ValueError("interactive delivery requires a confirmation criterion")
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


def _confirm_interactive_turn(
    handle: str,
    sent_at: float,
    *,
    run_json: RunJson,
    confirm: Callable[[float], bool] | None,
    ack_out_of_band: bool = False,
) -> str:
    deadline = time.monotonic() + TUI_DELIVERY_TIMEOUT_S
    next_resend_at = time.monotonic() + max(TUI_DELIVERY_RESEND_GRACE_S, 0)
    resends = 0
    accepted = False
    readiness = READINESS_READY
    while time.monotonic() < deadline:
        if confirm is not None and confirm(sent_at):
            return DELIVERY_CONFIRMED
        readiness = terminal_readiness(handle, run_json=run_json)
        if readiness == READINESS_UNKNOWN:
            # Not a swallowed prompt and not a working head: the pane cannot be asked at all.
            # Guessing either way here would hide the failure the caller has to act on.
            raise TuiDeliveryError(
                f"the pane could not be probed after the prompt was sent (resends={resends})"
            )
        if readiness == READINESS_BUSY:
            # The pane went to work on something: the prompt is in, whether or not the caller's
            # own proof of it has appeared yet.
            accepted = True
            if ack_out_of_band:
                return DELIVERY_ACCEPTED
        elif resends < TUI_DELIVERY_RETRIES and time.monotonic() >= next_resend_at:
            # Ready or held in a dialog: either way the pane is not working on this prompt, so it
            # is entered again. That is what carries a prompt past a dialog that swallowed it.
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
        f"(reason={'accepted-but-unconfirmed' if accepted else f'pane-stayed-{readiness}'}, "
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


def _screen_started_turn(screen: str, *, adapter: str = "") -> bool:
    if adapter == "claude":
        return bool(_CLAUDE_TURN_RE.search(screen))
    marker = screen.rfind("\u203a")
    status_area = screen[:marker] if marker >= 0 else screen
    return bool(_CODEX_WORKING_RE.search(status_area))
