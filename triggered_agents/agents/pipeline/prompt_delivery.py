"""Initial prompt delivery confirmation for TUI heads."""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import codex_sessions, terminal_session

TUI_DELIVERY_RETRIES = int(os.environ.get("TA_TUI_DELIVERY_RETRIES", "2"))
TUI_DELIVERY_CHECK_DELAY_S = float(os.environ.get("TA_TUI_DELIVERY_CHECK_DELAY_S", "0.5"))
TUI_DELIVERY_TIMEOUT_S = float(os.environ.get("TA_TUI_DELIVERY_TIMEOUT_S", "12"))
TUI_DELIVERY_RESEND_GRACE_S = float(os.environ.get("TA_TUI_DELIVERY_RESEND_GRACE_S", "1"))
TUI_DELIVERY_POLL_S = float(os.environ.get("TA_TUI_DELIVERY_POLL_S", "0.25"))
_WORKING_RE = re.compile(r"\b(?:working|thinking)\b", re.IGNORECASE)


class InjectDeliveryError(RuntimeError):
    """The TUI turn did not start after bounded delivery checks."""


@dataclass(frozen=True)
class DeliveryResult:
    signal: str
    resends: int


def _prompt_signature(prompt: str) -> str:
    for token in ("TASK.md", "REVIEW.md"):
        if token in prompt:
            return token
    words = re.findall(r"\S+", prompt)
    return " ".join(words[:6])


def _prompt_still_in_codex_composer(screen: str, prompt: str) -> bool:
    """Return true when the initial prompt is still visible in the Codex composer."""
    screen = terminal_session.strip_ansi(screen)
    marker = screen.rfind("\u203a")
    if marker < 0:
        return False
    composer = screen[marker:]
    signature = _prompt_signature(prompt)
    return bool(signature and signature in composer)


def _screen_started_turn(screen: str) -> bool:
    screen = terminal_session.strip_ansi(screen)
    marker = screen.rfind("\u203a")
    status_area = screen[:marker] if marker >= 0 else screen
    return bool(_WORKING_RE.search(status_area))


def confirm_initial_prompt_delivered(
    prompt: str,
    read_screen: Callable[[], str],
    send_enter: Callable[[], None],
    turn_started: Callable[[], str | None] | None = None,
    log_event: Callable[..., None] | None = None,
    check_delay_s: float = TUI_DELIVERY_CHECK_DELAY_S,
    timeout_s: float = TUI_DELIVERY_TIMEOUT_S,
    poll_s: float = TUI_DELIVERY_POLL_S,
    resend_grace_s: float = TUI_DELIVERY_RESEND_GRACE_S,
) -> DeliveryResult:
    started = turn_started or (lambda: None)
    log = log_event or (lambda **_: None)
    deadline = time.monotonic() + timeout_s
    next_resend_at = time.monotonic() + max(check_delay_s, resend_grace_s, 0)
    resends = 0
    last_reason = "no-start-signal"

    while time.monotonic() < deadline:
        signal = started()
        if signal:
            log(result="confirmed", signal=signal, resends=resends)
            return DeliveryResult(signal=signal, resends=resends)
        screen = read_screen()
        if _screen_started_turn(screen):
            log(result="confirmed", signal="screen-working", resends=resends)
            return DeliveryResult(signal="screen-working", resends=resends)
        in_composer = _prompt_still_in_codex_composer(screen, prompt)
        last_reason = "prompt-in-composer" if in_composer else "awaiting-start-signal"
        if in_composer and resends < TUI_DELIVERY_RETRIES and time.monotonic() >= next_resend_at:
            send_enter()
            resends += 1
            log(result="resend", resends=resends)
            next_resend_at = time.monotonic() + max(resend_grace_s, 0)
        sleep_for = min(max(poll_s, 0.01), max(deadline - time.monotonic(), 0.01))
        time.sleep(sleep_for)
    log(result="failed", reason=last_reason, resends=resends)
    raise InjectDeliveryError(
        f"inject не доставлен: turn не стартовал после {timeout_s:.1f}s "
        f"(reason={last_reason}, resends={resends})")


def deliver_initial_prompt(
    prompt: str,
    workspace: str,
    handle: str,
    wait_idle: Callable[[], None],
    send_prompt: Callable[[], None],
    send_enter: Callable[[], None],
    read_screen: Callable[[], str],
    log_event: Callable[..., None] | None = None,
    check_delay_s: float = TUI_DELIVERY_CHECK_DELAY_S,
    timeout_s: float = TUI_DELIVERY_TIMEOUT_S,
    poll_s: float = TUI_DELIVERY_POLL_S,
    resend_grace_s: float = TUI_DELIVERY_RESEND_GRACE_S,
) -> DeliveryResult:
    log = log_event or (lambda **_: None)
    wait_idle()
    sent_at = time.time()
    log(workspace=workspace, handle=handle, result="initial")
    send_prompt()

    def turn_started() -> str | None:
        return "session-user-turn" if codex_sessions.latest_user_turn_for(workspace, sent_at) else None

    return confirm_initial_prompt_delivered(
        prompt,
        read_screen,
        send_enter,
        turn_started=turn_started,
        log_event=lambda **fields: log(workspace=workspace, handle=handle, **fields),
        check_delay_s=check_delay_s,
        timeout_s=timeout_s,
        poll_s=poll_s,
        resend_grace_s=resend_grace_s,
    )
